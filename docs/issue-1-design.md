# Issue #1: 汎用録画エンジンとカメラ制御の分離設計

状態: 設計・調査の記録。実装と検証状況は [issue-1-implementation.md](issue-1-implementation.md) を参照。

調査日: 2026-10-21

レビュー反映日: 2026-10-22（提供された設計レビューR01〜R04）
対象: [Issue #1](https://github.com/ponpaku/kn20a-recorder-test/issues/1)、コミット `e66e5ddbc4a14bfc93d47dbc582500ce4e05062c`

## 1. 結論と範囲

録画処理の大半はそのまま利用可能。ただし、単に `Generic` 選択肢を足すだけでは不足する。プリセットによる開始ブロック、再接続後の結合条件、MP4検証・元データ削除を先に整理する。

今回実装する範囲は、単一入力・同時1録画のRTSP/TCPとRTMP Listen、制御なしのGenericと既存KN20A、既存Web UI/APIの互換性。ONVIF、SRT、複数カメラ同時録画、映像の自動再エンコードは含めない。将来追加できる境界だけ設ける。

汎用カメラで保証するのは、検証済みコーデック構成の入力から保存まで。機種名、RTSP対応表記、ONVIF対応表記だけで実機互換性を宣言しない。

## 2. 現行コードの確認結果

行番号は調査対象コミットに対する目安。実装時はメソッド名で追跡する。

| 箇所 | 確認した挙動 | 設計への影響 |
| --- | --- | --- |
| `RecorderManager._prepare_preset` / `_tick`（約691〜751行） | カメラ呼び出し失敗時はFalseとなり、FFmpeg起動に進まない。呼び出しと安定待ちがschedulerスレッド上 | 制御準備を非同期の別担当へ移す。録画可否は明示ポリシーで決める |
| `_start_process`（約753〜854行） | RTSP/RTMPの分岐と共通segment出力が同居。video 1本と全audioをコピー | 入力引数生成と録画出力の責務を分ける |
| `_finish_process`（約951行〜） | FFmpeg終了を検知して再起動。保存バイト数があれば完了へ進める | 無受信ハングと、有効映像の存在・欠落を別に監視する |
| `_remux_schedule`（約559〜654行） | 全MKVを無条件にconcat。既存MP4が有効と見なされるとMKVを削除 | manifestと互換性判定、出力と元ファイルの対応検証が必要 |
| `_valid_mp4`（約531行〜） | ファイル存在・サイズ・正のformat duration中心 | 映像なし・破損・一部欠落を見逃し得る |
| `AppState.__init__`（約2569行〜） | 常にKN20AのExposureClientとExposureGuardを構成 | factoryから制御方式別に構成する。GenericではKN20Aオブジェクトを生成しない |
| API/Web | capabilityがなく、露出・プリセットAPIを常に公開 | APIで機能検証、UIは同じcapabilityを使用する |
| 容量推定 | 保存先の直近録画の中央値を参照 | 入力ID別に分離し、結合時の二重保持も計算する |

## 3. 実験結果

### 3.1 環境・再現方法

Ubuntu 24.04（WSL）、Python 3.12、FFmpeg `6.1.1-3ubuntu5`、MediaMTX `v1.21.2`。
MediaMTX公式リリースのLinux amd64版を取得し、同リリースの `checksums.sha256` と一致を確認した。これは製品依存ではなく、試験用サーバーのみ。

```bash
python3 -B tools/investigate_issue1.py --mediamtx /path/to/mediamtx
# コーデックと検証処理だけを再現する場合:
python3 -B tools/investigate_issue1.py --mediamtx /path/to/mediamtx --media-only
```

試験は自動生成した128×96/10fps・2秒のH.264/AACなどを使用。ネットワークは127.0.0.1のみ、portは動的割当。既存のRecorderManagerを変更せず使用し、予約・出力・カメラモックを一時フォルダへ隔離。試験終了時はFFmpegとMediaMTXを回収する。実機カメラは使用していない。

### 3.2 録画と制御依存

| 実験 | 結果 |
| --- | --- |
| プリセット2を指定し、制御APIが失敗 | FFmpeg起動がブロックされることをモックで再現 |
| 同じ条件でプリセット未指定 | 制御APIへアクセスせず起動へ進む |
| RTSP/TCP、8秒予約 | completed、起動1回、MKV 3個→MP4 1個、約5.448秒、全体デコード成功 |
| RTSP/TCP、15秒予約中にpublisher停止・再開 | completed、起動3回、MKV 3個→MP4 1個、約4.946秒、全体デコード成功 |
| RTMP Listen、8秒予約 | completed、起動1回、MKV 2個→MP4 1個、約3.405秒、全体デコード成功 |

ネットワーク試験では全件カメラ制御呼び出し0回。短時間の試験であり、受信開始待ち・GOP待ち・通信断があるため、予約の壁時計時間とMP4時間は一致しない。これを「予約全体を欠落なく録れた」証拠にはしない。現行completedは欠落なしを意味しないことも確認できた。長時間運用、RTMP再接続、認証、実機の接続数制限は未検証。

### 3.3 コンテナとデータ安全性

| 入力・操作 | 実測結果 | 設計判断 |
| --- | --- | --- |
| H.264 + AAC | MP4へのstream copy成功 | 標準プロファイル |
| H.264、音声なし | 成功 | 音声を必須にしない |
| H.265 + AAC | 成功 | コンテナ互換を提供。ただし再生アプリの対応とは分ける |
| H.264 + G.711 A-law | MKV保存可能、MP4 mux失敗（exit 234） | 既定はMKV保持。無断の音声削除・変換はしない |
| H.264 + PCM s16le | 今回のFFmpegではMP4 copy成功 | 「PCMは必ずMP4不可」と固定判定しない。広い再生互換性は未検証 |
| H.264で128→160幅のMKVをconcat | 結合exit 0、デコードexit 0、MP4ヘッダー幅は先頭の128 | 成功コードだけでは単一構成の成果物を保証できない。構成変更でgroupを分ける |
| H.264→H.265を無条件concat | 結合exit 0。後段デコードはexit 0でもエラーログあり | codec混在を事前排除し、終了コード以外の検証も行う |
| 音声だけのMP4 | 現行 `_valid_mp4` がTrue | 映像stream/packetを検証する |
| faststart MP4を約70%へ切り詰め | 現行検証True、全体デコードはエラー（exit 183） | durationだけの検証を廃止する |
| 上記破損MP4を既存recording.mp4として配置、削除設定False | 現行finalizerがMP4を再利用し、正常な元MKVを削除 | 最優先の安全性改修。既存MP4を存在だけで信用しない |

最後の試験は `keep_mkv_after_remux=false`（「変換後MKVを保持しない」）の意味。実際に消したのは試験で生成した一時MKVのみ。

2026-10-22のレビュー追試では、終了済みの旧録画予約と有効な残存MKVを一時フォルダに配置した。現行ScheduleStore読込後は `pending`、schedulerの終了判定後は `missed`、起動時のfinalization投入は0回、MKV自体は残存、というR02の経路を再現した。物理PCの強制終了を使った復旧試験ではなく、保存済み中断状態からの現行コード経路の確認である。

### 3.4 公開済みCIの確認

[初回CI](https://github.com/ponpaku/kn20a-recorder-test/actions/runs/39231074994) は失敗。Windows/Python 3.12の2テストが、TEMPの短縮パス `RUNNER~1` と `.resolve()` 後の長いパスを文字列比較して失敗していた。残りのmatrixはfail-fastでcancel。録画機能の不具合と決めつけず、Pathの同一性比較へ変更する。matrixは `fail-fast: false` とし、OS全体の結果が残るようにする。ここでは調査のみでCIを修正・再実行していない。

調査後のUbuntu 24.04で現行単体・結合テスト62件は全件成功した。これは公開CIが成功したという意味ではない。今回追加したのは設計資料と調査ハーネスのみで、製品コード・既存テスト・CI設定は変更していない。

## 4. 責務と依存関係

```text
HTTP API / Web UI
       ↓
ScheduleRunner ──→ ControlCoordinator ──→ Preset adapter (None / KN20A)
       ↓                    ↑ capability/結果のみ
RecorderEngine ──→ InputAdapter (RTSP TCP / RTMP Listen)
       ↓
ArtifactStore → Finalizer → Validator

ExposureService ──→ Exposure adapter (None / KN20A)
       ↓
AnalysisSource (KN20A WebSocket / 既存RTSPの移植)
```

RecorderEngineはカメラ、プリセット、露出、VISCA、ONVIFをimportしない。入力アダプターもカメラを操作しない。プリセットはScheduleRunnerが調整する別工程。露出サービス停止・故障も録画の停止理由にしない。

大規模なプラグインローダーは作らず、まず組み込みfactoryとPython Protocolで十分。

| モジュール | 契約・担当 |
| --- | --- |
| `recorder_app/config.py` | version判定、正規化、移行dry-run、設定検証 |
| `domain.py` | RecordingPlan、StreamInfo、Capabilities、ControlResult、Artifact、エラー型 |
| `inputs/base.py`, `rtsp.py`, `rtmp.py` | `build_input_args()`、`probe_strategy()`、transport固有timeoutを生成 |
| `core/engine.py` | `start(plan)` / `stop(reason)` / `snapshot()`、FFmpeg生存・進捗監視 |
| `core/scheduler.py` | JST予約、重複排除、開始終了、再起動からの再開 |
| `orchestration/schedule_runner.py` | プリセット準備と録画の開始期限を調整 |
| `controls/base.py`, `none.py`, `kn20a.py` | PTZ/露出の個別契約、KN20A HTTP/VISCAの移植 |
| `exposure/controller.py`, `analysis.py` | 現行AEの計算・ガード・子プロセス回収を維持 |
| `core/artifacts.py`, `finalizer.py`, `storage.py` | manifest、互換性group、結合・検証、容量計算 |
| `web/http.py` | HTTP入出力、capability検証、互換API変換 |

既存 `recorder.py` はCLIの入口として残す。まず移動に留め、AEの測光式やVISCA番号体系を同時に作り直さない。

## 5. 設定と機能宣言

新規設定の概念例（未実装。現行config.jsonへそのまま投入しない）:

```json
{
  "schema_version": 2,
  "input": {
    "id": "camera-main",
    "type": "rtsp",
    "url": "rtsp://192.0.2.10/live/main",
    "transport": "tcp",
    "connect_timeout_seconds": 5,
    "read_timeout_seconds": 5,
    "probe": "best_effort",
    "video_track": 0,
    "audio_tracks": "all"
  },
  "controls": {
    "presets": {"driver": "none"},
    "exposure": {"driver": "none"}
  },
  "recording": {
    "output_dir": "recordings",
    "segment_seconds": 600,
    "preset_failure_policy": "continue",
    "preset_deadline_seconds": 10,
    "finalize": "copy_mp4_or_keep_mkv",
    "retain_sources": true,
    "verification": "packet_scan"
  }
}
```

KN20Aの場合はpresets/exposureの各driverを `kn20a` にし、それぞれendpoint/portを指定する。内部では同一接続先のKN20Aクライアントを共有できる。入力URLと制御IPの一致は要求しない。これにより将来RTSP + ONVIF presets + メーカー独自exposureを構成できる。

新規Generic設定例と新規KN20A設定例を別々に配布する。古い設定は後述の互換変換を行うため、schema_version欠落をGenericと見なさない。

Capabilityは少なくとも以下を分ける:

- `presets.recall` / `presets.save` / `presets.list`、ID形式・範囲
- `exposure.read` / `exposure.write` / `exposure.manual`、操作可能項目・単位
- `analysis_ae.supported` / `analysis_ae.available` / `reason`

`supported`（方式として対応）と `available`（現在到達可能）を区別する。KN20Aが一時オフラインでも未対応機種へ変わった扱いにしない。Genericではpresets/exposure/analysis_aeがすべて非対応で、録画は可能。

絞りの読出しは「命令した設定の読戻し」と「センサー等による物理実測」を区別する。KN20A Manualでも、HTTP/VISCA応答だけで物理実測とは保証しない。互換APIの `actual` を直ちに削除せず、新しい内部モデルでは `readback_kind` と有効モードを保持する。AEはKN20Aで既に確認した制御契約だけで提供し、ONVIF対応表記から自動的に有効化しない。

プリセットの内部IDは文字列にし、将来のONVIF opaque tokenに備える。KN20Aの数値検証（0〜254）とUIの常時候補0〜10はアダプター側の仕様。既存 `preset_number` は互換変換する。

## 6. プリセット・再接続・状態遷移

準備は開始15秒前から実行可能とするが、前の録画が同じカメラを占有している場合は送信しない。workerを独立させることは、占有制約を解除する意味ではない。

### 6.1 カメラ占有と送信の契約（R03）

- カメラ資源の識別子 `control_resource_id` は映像入力IDとは別に持つ。同じ物理カメラを指す制御アダプターは同じ資源を共有する。異なるURLやIP別名を物理的に同一と自動判定できるとは保証しない。
- 占有は接続開始を決定する時点から、録画FFmpegの終了をwaitで確認して区間を閉じるまで保持する。再接続待ちも占有期間とし、その間に次予約の準備を割り込ませない。後処理だけになれば占有を解放できる。
- 要求は予約ID、予約revision、execution generation、資源ID、送信期限を持つ。送信直前に同じ資源の調停ロック下で、予約の有効性、取消・変更、期限、実行世代、占有者を再検証する。検証と「送信権の確定」の間へ別予約の開始を割り込ませない。ネットワークの応答待ちは調停ロック外で行い、録画停止をブロックしない。
- 別予約が占有中なら未送信のままdeferする。9:00–10:00のAと10:00開始のBでは、9:59:45にBの準備を送らない。Aの受信プロセス終了を確認してからBに送信権を渡す。continueならBの録画開始を優先し画角未確認を記録し、requireならBの保存済み期限内で準備を待つ。境界でAの画角維持とBの開始時刻・画角を全て厳密に満たせるとは保証しない。
- 取消/期限切れのキュー要求は破棄し、送信待ちworkerも上記の再検証で拒否する。遅い応答は同じgenerationの結果としてのみ記録し、別予約の成功状態に転用しない。
- すでにカメラへ送信済みの移動は、予約取消やソケット切断だけでは取り消せない。送信済み要求は `in_flight`、確認不能な結果は `uncertain` として追跡する。カメラが停止/完了状態確認を提供する場合は利用するが、ACKだけで物理移動完了と見なさない。
- `uncertain` な古い操作がある間は、新たな自動画角命令を送らない。continueの録画自体は可能だが「画角未確定・移動継続の可能性」を記録する。画角確定を必須とするrequireは期限で失敗する。確定/停止を確認するか、利用者が不確定状態を明示解除するまで、自動的に安全確認済みへ戻さない。
- HTTP/APIの手動プリセットも同じ調停器を使う。録画中は既定409とし、明示的な手動上書き操作を将来提供する場合も予約準備と区別して監査する。AEの露出変更は画角の占有とは別の操作権として扱う。

保証するのは「無効な未送信要求を送らない」「前予約を別予約の命令で動かさない」「送信済み操作の不確実性を隠さない」。物理カメラへ渡った操作の完全な取消保証は受け入れ条件にしない。

### 6.2 失敗ポリシーと状態

| 方針 | 開始時にプリセット未成功 | 再接続時 |
| --- | --- | --- |
| `continue`（新規既定） | 録画を開始し、警告と未確認画角を記録 | 再呼出しをbest-effortで行いつつ受信再開。制御による待機を追加しない |
| `require`（明示指定） | 許容期限内だけ待機。開始時刻+10秒または終了時刻で打ち切り、失敗理由を残す | 同じ期限ポリシー。無期限に待機しない |
| legacy互換 | 従来の終了時刻までの再試行を維持し、UIに移行推奨表示 | 従来の再呼出し順序を維持 |

continue方針で録画開始後にプリセットを遅れて実行すると録画中に画角が動く。したがって、開始後の新たな再試行は既定で行わず、開始前に送信済みの移動が継続する可能性は警告する。明示的な再接続時の再呼出しは別の記録イベントとして扱う。

Genericの新規予約でプリセットを指定する入力は422で拒否する。保存済み予約が、後から制御方式を変更したことで非対応になった場合は、continueなら警告して録画、requireなら開始前検証で失敗させる。予約を黙って削除しない。

録画と後処理は別状態軸にする:

- capture: `pending → preparing → connecting → recording ↔ reconnecting → stopped`、`missed/failed`
- finalize: `not_started → queued → running → verified / kept_mkv / failed`
- outcome: `complete / partial / no_media / unknown`。制御警告と映像欠落は別項目

開始待ち、再接続、未知の受信区間をmanifestへ記録する。メディアのPTS時間を壁時計の連続録画時間と混同しない。欠落区間を推定したものはestimatedと表示する。FFmpegの終了コード0や予約終了だけでcompleteと判定しない。

## 7. 入力検査とプロセス監視

RTSPは予約を作るたびに必須probeしない。手動「入力を確認」は非同期ジョブとして提供し、既定は到達不能でも警告扱い。追加接続を嫌うカメラのため `probe=off` を設ける。録画中はメイン入力への追加probeを行わず、受信済みMKVを検査する。開始前probeが失敗しても録画試行を阻害しない。

RTMP Listenではprobe用に別listenerを立てない。録画FFmpegを唯一の受信口にし、閉じた最初のMKVを検査する。RTMPの接続待ちtimeout（秒）とRTSPのI/O timeout（μ秒）は、共通設定から各アダプターが変換する。単位を共有して誤適用しない。

probeの取得情報はcodec/type/order、profile/level、width/height/pix_fmt/SAR、time_base、audio sample_rate/channels/layout、extradata hash、duration/packet情報。RTSPのSDPとMKVではtime_base等が変わり得るため、結合fingerprintは保存済みファイル同士で比較する。bitrateだけの変動はgroup分割理由にしない。

FFmpegの `-progress pipe:1` を専用readerで回収し、受信進捗を監視する。起動中の生存と、映像を受信している状態を分離する。I/O timeoutに加えて、設定したstall期限を超えた場合は正常終了要求→terminate→kill→waitで回収し、終了時刻まで有限バックオフで再接続する。最初の接続待ちと録画中stallでは期限を分ける。stderr/stdoutを読み捨てず回収し、pipe詰まりを防ぐ。

予約終了は全状態で最優先し、再接続・probe・プリセット待ちをキャンセルする。プロセスごとにgenerationを管理し、終了済みworkerの応答で新しい試行状態を上書きしない。FFmpeg binaryとそのversionを記録する。

## 8. 分割・結合・安全な保持

### 8.1 manifest

予約フォルダ内に `manifest.json` を原子的に保存する。相対パス、予約ID、入力ID、attempt、連番、受信時刻、file size/hash、構成fingerprint、probe/validation結果、欠落イベント、生成物と元ファイル一覧を記録する。更新担当はArtifactStoreに統一する。

順序は時刻を含むファイル名の文字列sortではなくmanifestの単調連番で決める。attempt10以降や同秒開始、時刻補正、残存ファイルがあっても衝突しないIDとする。録画中ファイルは未確定とし、プロセス終了後に検査する。

同じプロセスの途中でcodecや解像度が変わる場合、同一MKV内に混在する可能性がある。ヘッダーprobeだけでは検出保証できない。ログの構成変更イベント、閉じたsegmentの検査、全体検証で不確かなファイルは `unknown/incompatible` として元データを保持する。変化を検出した時点で現在ファイルを閉じ、新しいattempt/groupへ切り替える。無検知の変更まで完全防止できるとは宣言しない。

### 8.2 MP4方針

標準の再生互換プロファイルはH.264/H.265 + AAC、または音声なし。これは受信可能codecの完全一覧ではない。PCMのようにmuxできてもプレーヤー互換性が異なる構成は警告する。G.711等の非対応音声は既定で元のMKVを保持する。映像再エンコード、音声削除、AAC変換は明示指定なしでは行わない。

連続する互換fingerprintのsegmentだけをgroup化する。group A→B→AをAだけまとめ直すと時系列を壊すため、非連続groupを統合しない。変更なしなら従来通りMP4 1本。変更があればgroupごとのMP4とmanifest、またはMKV保持とし、「一つにする」ための無断再圧縮はしない。UIは複数出力の理由を表示する。

GOP境界のため、stream copyでsegment秒数どおりの分割やフレーム単位の開始を保証しない。通常のVBR/fps揺らぎだけで異常扱いにしない。stream order/count、codec/extradata、解像度、sample rate、time_base等の変更は保守的に分離する。

### 8.3 検証と削除条件（最優先）

1. 出力先の容量と元ファイル一覧・fingerprintを確定する。検査不能なファイルを黙って除外しない。
2. `.partial` へ変換し、元データは保持する。
3. 必須映像streamとpacket、選択した音声、groupの順序・packet数・時間範囲を検証する。短い出力や欠落は異常。音声のpadding/時刻丸めを含む許容値は実験から決定し、固定の「duration>0」へ戻さない。
4. 最低限は全packetを走査し、破損・欠落・重大なエラーログを検出する。先頭/末尾だけのprobeはスクリーニングであり完全検証ではない。
5. `verification=full_decode` は全体デコードも行う。終了コード0でもエラーログやframe不足があれば不合格とする。デコード不能/未対応でも原本は保持する。
6. 検証済み出力をatomic replaceし、出力hashと元ファイルhash一覧を含む検証済みmanifestを先に永続化する。
7. 原本削除は `retain_sources=false` かつfull_decodeを含む検証成功時のみ。新規設定は保持を既定にする。検証処理のコスト・進捗をUIへ表示する。

既存recording.mp4の再利用も、manifestのhashと元ファイル集合が一致し、同じ検証を通った場合だけ許可する。旧版生成物で証跡がない場合は破壊的cleanupを行わず、再検証または別名生成する。強制終了がmanifest確定と削除の間に起きても、次回は削除済みファイルを正常に扱えるよう、ファイルごとに削除完了を記録する。

原本にはMKVだけでなく、旧版の `part_*.mp4` も含める（R01）。保持ポリシーと検証済み成果物との対応証跡をファイル種別にかかわらず適用し、legacy MP4を無条件に削除する経路を残さない。

容量不足・破損・変換失敗・構成変更・署名不一致・検査timeoutでは原本を一切自動削除しない。capture成功/finalize失敗を別々に表示し、再実行可能にする。

### 8.4 容量と待ち行列

録画開始時は、残り録画量だけでなく、既存分を含む成果物の追加サイズと予備領域を見込む。結合直前にも再検査する。入力ID別の実績中央値を使い、ID不明の旧実績を新規入力へ混ぜない。入力変更時は統計を無効化する。

後処理は同時1件を既定とし、録画を優先する。長いfull_decodeやfaststartが次予約を遅延させない。空き容量低下はUIへ早めに通知し、録画中の停止は確定MKVを保持する。安全性を保ったまま無限に録画できるわけではない。

### 8.5 起動時の復旧契約（R02、公開前の必須条件）

起動時は、予約をpending/missedへ書き換える前にArtifactStoreとの照合を行う。ScheduleStoreの読込だけで `starting/recording` の証拠を消さない。元の状態を `previous_capture_state` として保持し、RecoveryCoordinatorが状態遷移を確定する。

1. 既存録画プロセスの有無を確認し、単一起動権を確保する。自分が管理していないプロセスや、まだ書込み中か不明なファイルは確定原本として扱わない。
2. 保存先内の予約output_dirとmanifestを照合する。旧版では予約IDに対応するフォルダ、`part_*.mkv` と `part_*.mp4` を読取り専用で探索する。保存先外のパスや別予約のファイルを推測で採用しない。
3. 残存物を `valid/partial/unreadable/unknown` に分類する。単にファイルがない場合と、アクセス不能・manifest破損で有無が不明な場合を区別する。未確定・破損ファイルも消さない。
4. 予約終了後でも原本があればcaptureを終了済みとして後処理/復旧へ進める。読める区間は互換性を検証して結合できるが、読めない区間を黙って除外してcompleteにしない。partial/unknownと原本を残す。
5. 予約期間が残っていれば、確定済みsegmentは保持してattemptの最大値より大きい新しい世代で受信を再開する。再開時のプリセット方針も予約に保存した契約を使う。
6. 原本がなく、管理下の録画プロセスもなく、終了済みの場合のみmissed/no_mediaを判定する。探索や検査が失敗した場合はrecovery_requiredとし、missedと断定しない。
7. finalize中断・verified後cleanup中断もmanifestの世代、出力hash、原本集合から再判定する。キューの予約ID/成果物世代は一意にし、再起動を繰り返しても重複結合・上書き・無証跡の削除を行わない。

その後に通常のschedulerを開始する。初期照合の判断が必要な予約だけを保留し、長いメディア検証は復旧キューへ渡して、無関係な次予約をブロックしない。監視APIは復旧中の存在・進捗・保持ファイルを表示する。

## 9. APIとUI

- `GET /api/state` にcapabilities、control availability、input health、capture/finalize/outcome、warningsを追加。旧キーは互換期間保持。
- 新規の非対応操作は `422 {code:"unsupported_capability", capability:"presets.recall"}`。一時到達不能は503、競合は409、値不正は400。KN20Aの既存ルートは互換ハンドラーからアダプターを呼ぶ。
- Genericではプリセット候補は「指定なし」、露出・AEは「この制御方式では非対応」と表示。露出APIの自動pollも止める。到達不能とは見分ける。
- 入力方式とPTZ/露出方式は独立して表示。ONVIF/SRTは未実装のまま選択可能にしない。
- 予約にプリセット失敗ポリシーを表示し、continue時は未確認画角の警告を録画状態から分離。
- 既存の編集中入力をpollで上書きしない挙動を維持する。
- API・画面・ログのURLはuser/passwordとtoken等を伏せる。認証情報を含むFFmpegコマンド全体をログへ出さない。

## 10. 後方互換性と移行

schema_versionなしの現行configをv1として読み、メモリ上でv2へ正規化する。読込だけでファイルを上書きしない。camera_ip/visca_port→KN20A制御、input_mode/input_url/listen_url→入力、既存の分析URL→KN20Aの分析設定へ移す。未知キーは警告して保持し、既知キー不正を無視して起動しない。

旧予約ID、保存先、JST、プリセット番号、既存AE状態と露出補正、segment秒数、再圧縮なしの方針を維持する。旧設定のプリセット待機はlegacy互換にし、新規v2だけcontinueを既定にする。移行画面で差と推奨値を説明する。

### 10.1 予約単位の実行契約（R04）

新規予約には有効な `preset_failure_policy` と期限値、`contract_version`、選択したpreset参照/制御資源を保存する。設定ファイルは作成時の既定値としてだけ使用し、実行時に現在の既定値を読み直して既存予約の方針を変えない。policyが省略された予約作成APIも、保存前にその時点の既定値を展開する。

既存のpolicyなし予約は、設定ファイルをv2へ移行しても、常に `legacy` と解釈する。読込時のメモリ上の補完でもよいが、次に保存する際はlegacy由来であることを記録する。新しい設定のcontinueを推測で埋めない。変更する場合は予約更新として明示し、変更前後・revision・実行世代を記録する。既に実行中の契約は更新で差し替えない。

| ケース | 保存・実行する方針 |
| --- | --- |
| policyなしの旧未実行予約＋v1設定 | legacy |
| 同じ旧予約＋設定だけv2へ移行 | legacyのまま |
| v2で新規作成、APIでpolicy省略 | 作成時の既定値を予約へ展開・保存 |
| 保存済みcontinue/require予約＋後日の設定変更 | 予約に保存したpolicyと期限を維持 |
| 利用者が旧予約の方針変更を明示 | revision更新、次の実行世代から適用 |

プリセット参照先を特定できない旧予約はKN20A旧設定の資源へ移行する。設定からその制御資源を削除する操作では、影響する未実行予約を表示し、参照を黙って別カメラへ付け替えない。

旧 `keep_mkv_after_remux=false` は保持ポリシーに引き継ぐが、安全性検証を省略する権限とは解釈しない。未検証既存MP4からの原本削除はv1でも停止する。この安全性変更とfull_decodeによる後処理時間増加はリリースノートへ明記する。

明示的な移行コマンドはdry-runで差分を出し、apply時に設定/予約/AE状態をバックアップする。変更は単一入力がidleの時だけ適用する。ロールバック用に元ファイルは残す。移行後ファイルを旧版が読める保証はしない。

## 11. 実装順序と受け入れ試験

| 段階 | 成果物 | 終了条件 |
| --- | --- | --- |
| 0: 安全性と試験基盤 | MP4検証・既存成果物再利用・原本削除の修正、CIパス比較修正 | 破損/音声のみ/欠落出力で原本が残る。全matrixの結果を取得 |
| 1: 境界抽出 | DTO、InputAdapter、制御Protocol、KN20A移植 | 現行62テストとAE往復シミュレーション、旧設定fixtureが回帰しない |
| 2: Genericとcapability | None adapter、factory、API/UI gating | KN20Aクライアント未生成・通信0回で予約からMP4まで完走 |
| 3: 準備の分離 | ControlCoordinator、continue/require/legacy、generation | 遅い制御が録画終了/次予約をブロックしない。期限・キャンセルの試験 |
| 4: artifactsと復旧 | manifest、group化、検証・再実行・容量 | 途中変更/破損/停電/ディスク不足でも原本保持、同じ録画を二重変換しない |
| 5: 公開準備 | 設定例、対応表、移行資料、実機検証 | テスト結果と実機未検証条件を明示 |

段階3/4を後回しに実装しても、R02〜R04の回帰を省いた状態ではIssue完了・正式公開としない。実装順序と公開受け入れ条件は別である。

### 11.1 レビュー由来の必須回帰

| 指摘 | 条件 | 成功条件 |
| --- | --- | --- |
| R01 | 破損/音声のみ/短尺の既存MP4、MKVと旧分割MP4の双方 | 証跡・検証がなければどちらの原本も削除しない |
| R01 | 検証後〜削除前、各ファイル削除途中に強制終了 | manifestから再判定し、未検証原本は保持 |
| R02 | recording/starting中断→予約終了後に起動、manifestあり/なし | 残存MKV/旧MP4を見つけて復旧/後処理へ進む。missedへ直行しない |
| R02 | 破損末尾、アクセス不能、繰返し再起動 | partial/unknownを表示し原本保持。重複後処理をしない |
| R03 | 接するA/B予約、B準備時刻がA録画中、Aが停止処理中 | Aの終了確認までBのプリセット送信0回 |
| R03 | 待機中/送信権取得直前の取消・予約変更・世代更新・期限切れ | 未送信要求を送らず、古い結果で新予約を更新しない |
| R03 | 送信後に取消、ACK遅延、物理移動完了を確認不能 | 不確実性を記録。確認なしに停止済みとせず、新たな自動命令を送らない |
| R04 | policyなし旧予約→設定だけv2へ移行 | legacyのまま実行 |
| R04 | 新規予約作成→既定値変更→実行 | 作成時に保存したpolicyと期限で実行 |
| CI | Windows/Ubuntu×Pythonの6 matrix | fail-fastによるcancelで検証を欠落させず、全6件が成功 |

単体試験は引数、単位、capability入力、設定変換、ポリシー、時計/期限、fingerprintを対象にする。結合試験は実FFmpegとloopback MediaMTXで以下を実行する。

- RTSP/TCP + Generic、RTMP Listen + Genericの予約→分割→最終出力、音声あり/なし。
- publisher切断/再開、server再起動、最初から到達不能、RTMP publisherの遅延/再接続、無受信ハング。
- KN20A HTTP/VISCAモックの正常/拒否/timeout/遅い完了。AEとプリセットの競合、期限後の応答。
- G.711、H.265、audio track数/順序/サンプルレート変更、解像度/codec変更、同一segment内の変更。
- 作成済みMP4が古い/音声のみ/途中切断、結合対象の不足・余分なファイル、10回以上の再試行の順序。
- 録画中、結合中、検証後、manifest保存前後、削除途中のプロセス強制終了と再開。
- 容量不足、書込み拒否、ファイルロック、Windows短縮パス/Unicode、Ubuntu SIGTERM/systemd。

Windows 3.10/3.12・Ubuntu 22.04/24.04のCIを使用する。数十秒のネットワークfixtureは任意の結合テストjobに分離し、通常の単体テストに外部ダウンロードを強制しない。MediaMTXは版とchecksumを固定する。現行の調査ハーネスは証拠の再現用で、製品の受け入れ試験をすべて実装したものではない。

## 12. 参考資料と判断の限界

- [FFmpeg concat仕様](https://github.com/FFmpeg/FFmpeg/blob/master/doc/demuxers.texi#concat): 入力のstream構成等の一致が前提。実験の「exit 0」よりこの前提を優先して設計する。
- [FFmpeg segment muxer](https://ffmpeg.org/ffmpeg-formats.html#segment_002c-stream_005fsegment_002c-ssegment): GOPに依存する分割の制約。
- [FFprobe](https://www.ffmpeg.org/ffprobe.html): streams/format/data hash/packet情報を取得するための一次資料。
- [FFmpeg protocols](https://www.ffmpeg.org/ffmpeg-protocols.html): RTSP/RTMP等の入力オプション。実際の6.1.1の `-h demuxer=rtsp` と `-h protocol=rtmp` でも単位を確認した。
- [ONVIF Profiles](https://www.onvif.org/profiles/) / [Profile T](https://www.onvif.org/profiles/profile-t/): プロファイルには条件付き機能もあり、ONVIF表記からPTZ/露出の全操作を推測しない。
- [MediaMTX publish/read](https://mediamtx.org/docs/features/publish-and-read): 実機の代わりにRTSP配信を用意する試験fixtureとして使用。

文書の設計案と今回の実測を区別する。別バージョンのFFmpeg、機種固有認証、各社RTSP/ONVIFの実装差、長時間安定性、同一segment内の構成変化への完全な検出は、今回の短時間fixtureでは証明していない。
