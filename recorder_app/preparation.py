"""Non-blocking preset preparation with capture ownership and dispatch fencing."""
from contextlib import contextmanager
from datetime import datetime
import threading
import time


class ControlConflict(RuntimeError): pass


class PresetCoordinator:
    def __init__(self, camera, store, config, now):
        self.camera, self.store, self.config, self.now = camera, store, config, now
        self.lock = threading.RLock()
        self.owner = None
        self.jobs = {}
        self.uncertain = any(item.get("control_in_flight") or item.get("control_uncertain") for item in store.list())
        self.closed = False
        self.in_flight = None

    def invalidate(self, identifier):
        with self.lock:
            self.jobs.pop(identifier, None)

    def set_owner(self, identifier):
        with self.lock:
            self.owner = identifier

    def acknowledge(self):
        with self.lock:
            if self.in_flight is not None:
                raise ControlConflict("制御要求はまだ実行中です")
            self.uncertain = False
            for item in self.store.list():
                if item.get("control_uncertain") or item.get("control_in_flight"):
                    self.store.update(item["id"], control_uncertain=False, control_in_flight=False)

    def shutdown(self):
        with self.lock:
            self.closed = True
            self.jobs.clear()

    def ready(self, item):
        if item.get("preset_number") is None:
            return True
        now = self.now()
        policy = item.get("preset_failure_policy", "legacy")
        start = datetime.fromisoformat(item["start"])
        end = datetime.fromisoformat(item["end"])
        with self.lock:
            job = self.jobs.get(item["id"])
            if job and job["revision"] != item.get("revision", 0):
                self.jobs.pop(item["id"], None)
                job = None
            bound = item.get("preset_resource")
            configured = self.config.get("controls",{}).get("presets",{"driver":"none"})
            resource_changed = bound is not None and bound != configured
            if resource_changed:
                self.store.update(item["id"], control_warning="予約作成後に制御接続先が変更されました")
            if job and job["state"] == "success":
                return True
            if self.camera is None:
                self.store.update(item["id"], control_warning="プリセット制御は非対応です")
                if policy == "continue":
                    return True
            if policy == "require" and (now - start).total_seconds() >= float(item.get("preset_deadline_seconds", 10)):
                self.store.update(item["id"], status="failed", last_error="プリセット準備が期限を超えました")
                return False
            if now >= end:
                return False
            can_send = (not resource_changed and self.camera is not None and not self.closed and not self.uncertain
                        and self.in_flight is None and self.owner in (None, item["id"]))
            # Once capture is connecting/recording, do not send a deferred preparation.
            can_send = can_send and item.get("status") not in ("starting", "recording", "stopping")
            if job and job["state"] == "failure" and time.monotonic() < job["retry_at"]:
                can_send = False
            if can_send and (job is None or job["state"] == "failure"):
                job = {"state": "queued", "revision": item.get("revision", 0), "sent": False,
                       "preset": item["preset_number"], "retry_at": 0, "done": threading.Event()}
                self.jobs[item["id"]] = job
                threading.Thread(target=self._run, args=(item, job), daemon=True).start()
            if policy == "continue" and now >= start:
                self.store.update(item["id"], control_warning="画角未確認で録画を継続します" + ("（制御接続先変更）" if resource_changed else ""))
                return True
            return False

    @contextmanager
    def authorize(self, item, job):
        with self.lock, self.store.lock:
            current = self.store.get(item["id"])
            now = self.now()
            if (self.closed or self.uncertain or self.jobs.get(item["id"]) is not job
                    or not current or current.get("revision", 0) != job["revision"]
                    or current.get("status") not in ("pending", "reconnecting")
                    or now >= datetime.fromisoformat(current["end"])
                    or self.owner not in (None, item["id"]) or self.in_flight is not None):
                raise ControlConflict("無効・競合したプリセット要求を破棄しました")
            if current.get("preset_failure_policy") == "continue" and current.get("status") != "reconnecting" and now >= datetime.fromisoformat(current["start"]):
                raise ControlConflict("録画開始後の遅延プリセットを破棄しました")
            if current.get("preset_failure_policy") == "require" and (now - datetime.fromisoformat(current["start"])).total_seconds() >= float(current.get("preset_deadline_seconds", 10)):
                raise ControlConflict("プリセット要求の期限が切れました")
            job["sent"] = True
            self.in_flight = job
            self.store.update(item["id"], control_in_flight=True)
            yield

    def _run(self, item, job):
        try:
            self.camera.recall_preset(job["preset"], authorize=lambda: self.authorize(item, job))
            time.sleep(max(0, float(self.config.get("preset_settle_seconds", 2))))
            with self.lock:
                self.store.update(item["id"], control_in_flight=False, control_uncertain=False)
                if self.jobs.get(item["id"]) is job:
                    job["state"] = "success"
                    self.store.update(item["id"], preset_recalled_at=self.now().isoformat(), control_warning="")
        except Exception as exc:
            with self.lock:
                if job["sent"]:
                    self.uncertain = True
                    self.store.update(item["id"], control_in_flight=False, control_uncertain=True)
                if self.jobs.get(item["id"]) is job:
                    job.update(state="failure", retry_at=time.monotonic() + float(self.config.get("retry_seconds", 5)))
                    self.store.update(item["id"], control_warning=str(exc))
        finally:
            with self.lock:
                if self.in_flight is job:
                    self.in_flight = None
                job["done"].set()

    def manual(self, operation):
        with self.lock:
            if self.owner or self.in_flight or self.uncertain:
                raise ControlConflict("録画・準備中または画角未確定のため操作できません")
            marker = object()
            self.in_flight = marker
        try:
            return operation()
        except Exception:
            with self.lock:
                self.uncertain = True
            raise
        finally:
            with self.lock:
                if self.in_flight is marker:
                    self.in_flight = None
