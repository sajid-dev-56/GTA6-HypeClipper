"""
Autonomous 24/7 Scheduler Engine (scheduler.py)
Part of GTA6-HypeClipper Pipeline.

Provides automated periodic livestream/VOD monitoring, hype clipping,
and optional daily automated uploading to YouTube Shorts.
Can be controlled via Web UI or run as a standalone background daemon.
"""

import datetime
import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("GTA6-HypeClipper.Scheduler")

CONFIG_FILE = "scheduler_config.json"


class SchedulerManager:
    """Thread-safe background scheduler for daily automated clipping & uploading."""

    def __init__(self, config_path: str = CONFIG_FILE):
        self.config_path = config_path
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._is_executing = False
        self._load_config()

    def _default_config(self) -> Dict[str, Any]:
        return {
            "enabled": False,
            "target_url": "https://www.youtube.com/watch?v=qSyo5d-dVzA",
            "preset": "speed_cam",
            "interval_hours": 24,
            "max_clips": 3,
            "auto_upload": False,
            "privacy": "unlisted",
            "last_run": None,
            "next_run": None,
            "total_runs": 0,
            "total_clips_created": 0,
            "total_clips_uploaded": 0,
            "history": [],
        }

    def _load_config(self):
        with self._lock:
            if os.path.isfile(self.config_path):
                try:
                    with open(self.config_path, "r", encoding="utf-8") as f:
                        self.config = json.load(f)
                        # Fill missing keys from default
                        defaults = self._default_config()
                        for k, v in defaults.items():
                            self.config.setdefault(k, v)
                except Exception as e:
                    logger.error(f"Failed to load scheduler config: {e}. Reverting to defaults.")
                    self.config = self._default_config()
            else:
                self.config = self._default_config()
                self._save_config_locked()

    def _save_config_locked(self):
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to write scheduler config: {e}")

    def save(self):
        with self._lock:
            self._save_config_locked()

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.config.get("enabled", False),
                "is_executing": self._is_executing,
                "target_url": self.config.get("target_url", ""),
                "preset": self.config.get("preset", "speed_cam"),
                "interval_hours": self.config.get("interval_hours", 24),
                "max_clips": self.config.get("max_clips", 3),
                "auto_upload": self.config.get("auto_upload", False),
                "privacy": self.config.get("privacy", "unlisted"),
                "last_run": self.config.get("last_run"),
                "next_run": self.config.get("next_run"),
                "total_runs": self.config.get("total_runs", 0),
                "total_clips_created": self.config.get("total_clips_created", 0),
                "total_clips_uploaded": self.config.get("total_clips_uploaded", 0),
                "history": self.config.get("history", [])[-20:],  # last 20 records
            }

    def update_config(self, updates: Dict[str, Any]) -> Dict[str, Any]:
        with self._lock:
            for k in [
                "enabled",
                "target_url",
                "preset",
                "interval_hours",
                "max_clips",
                "auto_upload",
                "privacy",
            ]:
                if k in updates:
                    self.config[k] = updates[k]

            # Recalculate next run if enabled
            if self.config.get("enabled", False):
                if not self.config.get("next_run"):
                    next_time = datetime.datetime.now() + datetime.timedelta(
                        hours=self.config.get("interval_hours", 24)
                    )
                    self.config["next_run"] = next_time.isoformat()
            else:
                self.config["next_run"] = None

            self._save_config_locked()
            logger.info(f"Scheduler configuration updated: enabled={self.config.get('enabled')}")
        return self.get_status()

    def start(self):
        """Start the background monitoring thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="SchedulerWorker")
        self._thread.start()
        logger.info("Scheduler daemon worker thread started.")

    def stop(self):
        """Stop the background monitoring thread."""
        self._stop_event.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("Scheduler daemon worker thread stopped.")

    def trigger_now(self) -> Dict[str, Any]:
        """Manually trigger a run immediately in a separate background thread."""
        if self._is_executing:
            return {"status": "error", "message": "A scheduled task is already currently running."}

        exec_thread = threading.Thread(target=self._execute_pipeline_job, daemon=True)
        exec_thread.start()
        return {"status": "success", "message": "Pipeline run triggered manually."}

    def _run_loop(self):
        """Main periodic check loop."""
        while not self._stop_event.is_set():
            try:
                should_run = False
                with self._lock:
                    if self.config.get("enabled", False) and not self._is_executing:
                        next_run_str = self.config.get("next_run")
                        if next_run_str:
                            try:
                                next_run_dt = datetime.datetime.fromisoformat(next_run_str)
                                if datetime.datetime.now() >= next_run_dt:
                                    should_run = True
                            except Exception:
                                should_run = True
                        else:
                            should_run = True

                if should_run:
                    self._execute_pipeline_job()

            except Exception as e:
                logger.error(f"Error in scheduler loop: {e}")

            # Sleep in small slices to respond promptly to stop events
            for _ in range(30):
                if self._stop_event.is_set():
                    break
                time.sleep(1)

    def _execute_pipeline_job(self):
        """Executes the pipeline on the configured target URL."""
        if self._is_executing:
            return
        self._is_executing = True

        run_record = {
            "timestamp": datetime.datetime.now().isoformat(),
            "target_url": "",
            "clips_count": 0,
            "uploaded_count": 0,
            "status": "in_progress",
            "error": None,
        }

        try:
            with self._lock:
                target_url = self.config.get("target_url")
                preset = self.config.get("preset", "speed_cam")
                max_clips = self.config.get("max_clips", 3)
                auto_upload = self.config.get("auto_upload", False)
                privacy = self.config.get("privacy", "unlisted")
                interval_hours = self.config.get("interval_hours", 24)
                run_record["target_url"] = target_url

            logger.info(f"--- [SCHEDULER RUN START] Target: {target_url} (Preset: {preset}) ---")

            # Execute pipeline
            from main import run_pipeline, load_yaml_config
            import argparse

            args = argparse.Namespace(
                url=target_url,
                preset=preset,
                max_clips=max_clips,
                upload=auto_upload,
                privacy=privacy,
                config="config.yaml",
                output_dir="output",
                whisper_model=None,
                dry_run=False,
            )

            run_pipeline(args)

            run_record["status"] = "success"
            run_record["clips_count"] = max_clips

            logger.info(f"--- [SCHEDULER RUN COMPLETED SUCCESSFULLY] ---")

        except Exception as e:
            logger.exception(f"Scheduler execution encountered error: {e}")
            run_record["status"] = "error"
            run_record["error"] = str(e)
        finally:
            now = datetime.datetime.now()
            with self._lock:
                self._is_executing = False
                self.config["last_run"] = now.isoformat()
                interval = self.config.get("interval_hours", 24)
                next_time = now + datetime.timedelta(hours=interval)
                self.config["next_run"] = next_time.isoformat()
                self.config["total_runs"] = self.config.get("total_runs", 0) + 1
                if run_record["status"] == "success":
                    self.config["total_clips_created"] = self.config.get("total_clips_created", 0) + run_record["clips_count"]
                
                # Maintain history
                history = self.config.setdefault("history", [])
                history.append(run_record)
                if len(history) > 50:
                    self.config["history"] = history[-50:]
                self._save_config_locked()


# Global singleton instance
scheduler_instance = SchedulerManager()
