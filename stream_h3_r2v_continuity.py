# SPDX-License-Identifier: GPL-3.0-only
"""Continuous MiniMax H3 Ref2V stream with configurable story clips.

Generation is slower than playback at 24 fps, so the stream is retimed on the
way out: the frames are authored at 24 fps and played at a lower rate, with the
audio slowed by the same factor, which preserves pitch. The break-even playback
rate is ``length / generation seconds``, so it must be derived from a
back-to-back measurement, never from a single best run -- planning against a
best case is exactly how this script ended up defaulting to an ``--fps`` it
could not sustain. Use ``profile_h3_nodes.py`` to re-measure after any change.

Retiming happens on the way to the muxer, never in the generation loop, so it
costs the generator nothing (a whole clip re-encodes in ~0.7 s).

Each completed clip contributes its last usable frame to the next prompt.  The
default uses that frame only as a frame-0 H3 guide, which avoids recursively
amplifying its colour and contrast as a Ref2V identity token. The optional dual
mode also binds it as the last ``<Picture N>`` reference. Original character
images remain true Ref2V identity references in both modes.

Continuity deliberately reduces the ComfyUI pipeline to one prompt: a future
prompt cannot reference a frame that has not been generated yet.  Turn
continuity off in the control UI to restore ``--pipeline`` parallel queueing.

Architecture:

    producer thread   POST /prompt -> poll /history -> clip mp4  -> queue
    feeder  thread    queue -> ffmpeg (retime, mpegts) -> stdin of ->
    output  ffmpeg    -re paced -> http/udp

``-re`` on the output paces the stream at real time, so a full pipe blocks the
feeder: that backpressure is what bounds memory, and a queue that drains to zero
is exactly the "generation fell behind" signal this script is meant to expose.
Each clip is deleted the moment it has been fed.

The producer keeps ``--pipeline`` prompts in ComfyUI's own queue rather than
submitting one and waiting for it, so the GPU is never idle across the gap
between a job finishing and the next one being accepted.

Open the printed URL in VLC.
"""

from __future__ import annotations

import argparse
import collections
import copy
import os
import queue
import random
import re
import select
import shutil
import socket
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import time
import urllib.error
from urllib.parse import urlparse

from submit_h3 import get, post, to_api  # noqa: E402

import json

SCRATCH_PREFIX = "stream_tmp/clip"
CONTINUITY_SUBDIR = "fasth3_continuity"
CONTINUITY_SENTENCE = (
    "Use the last reference image <Picture {picture}> to start the story. "
    "Begin exactly from the moment shown in <Picture {picture}> and continue "
    "without a cut. Preserve the same framing, character positions, clothing, "
    "lighting, environment, and camera direction."
)
CONTINUITY_GUIDE_SENTENCE = (
    "Continue seamlessly from the supplied first-frame guide. Begin exactly "
    "from that frame without a cut. Preserve the same framing, character "
    "positions, clothing, lighting, environment, and camera direction."
)
CONTINUITY_CAST_TOKEN = "__H3_LAST_FRAME_REFERENCE__"

# VHS_VideoCombine writes a metadata PNG of the first frame and, when audio is
# attached, a silent intermediate mp4 alongside the muxed one. Both are dead
# weight for a clip that is fed once and deleted, and the PNG is a full libpng
# encode per clip. These two flags are read out of the workflow "extra" block,
# which reaches the node through /prompt's extra_data.
VHS_EXTRA_DATA = {
    "extra_pnginfo": {"workflow": {"extra": {"VHS_MetadataImage": False,
                                             "VHS_KeepIntermediate": False}}}
}

# How ComfyUI writes the clip. Both stock nodes spend their time in Python
# rather than in the encoder -- ffmpeg alone does this payload in 0.21 s -- so
# the ranking is about how few Python passes each makes over the frames, not
# about the codec. h3fast is the custom node in
# custom_nodes/h3_fast_writer, which converts in chunks and muxes the audio in
# the same pass. The file is re-encoded by feed_clip a second later anyway, so
# the intermediate only has to be cheap and not lossy enough to compound.
# h3fast additionally hands the encode to a background thread and returns, so
# ComfyUI can start the next prompt instead of holding an idle GPU for the
# write. Its reported filename therefore names a file that does not exist yet;
# the node renames the clip into place when it is complete, which is why
# Producer.collect waits for the name rather than trusting /history alone.
WRITERS = {
    "h3fast": ("H3FastWriteVideo",
               {"crf": 16, "preset": "veryfast", "chunk_frames": 32,
                "async_write": True}),
    "vhs-nvenc": ("VHS_VideoCombine", {"format": "video/nvenc_h264-mp4",
                                       "pix_fmt": "yuv420p", "bitrate": 10,
                                       "megabit": True, "save_metadata": False}),
    "vhs-x264": ("VHS_VideoCombine", {"format": "video/h264-mp4",
                                      "pix_fmt": "yuv420p", "crf": 16,
                                      "save_metadata": False,
                                      "trim_to_audio": False}),
}

DEFAULT_PROMPTS = [
    """integrated_multimodal_description: [Shot 1] Live-action, cinematic, a medium shot frames a night market alley in the rain, red lanterns strung overhead and reflections breaking on the wet stone. The camera pushes in with small amplitude at slow speed as a vendor in a canvas apron turns skewers over a charcoal grill, sending sparks upward. [Shot 2] At 00:07.000, the shot cuts to a close-up of the grill, fat dripping onto the coals and flaring.

overall_soundscape: Rain patters on canvas awnings while charcoal hisses and crackles under dripping fat. Distant conversation and the clatter of tongs carry down the alley.

non_diegetic_music: N/A""",
    """integrated_multimodal_description: [Shot 1] Live-action, cinematic, a wide shot frames an empty subway platform at night, fluorescent tubes flickering along the curved ceiling. The camera trucks right with small amplitude at slow speed past tiled columns as warm air pushes litter along the platform edge. [Shot 2] At 00:08.000, the camera cuts to a low shot of the tunnel mouth as headlights grow and a train sweeps through.

overall_soundscape: A low ventilation hum fills the station, broken by the rising roar of an approaching train and the squeal of brakes on steel.

non_diegetic_music: A slow synthesizer drone with a single repeating bass note, rising in volume as the train arrives.""",
    """integrated_multimodal_description: [Shot 1] Live-action, cinematic, a close shot frames rain running down a workshop window at dusk, tools hanging in silhouette behind the glass. The camera pulls out with small amplitude at slow speed to reveal a wooden bench covered in brass parts and open notebooks. [Shot 2] At 00:09.000, the shot transitions to an overhead close-up of hands sorting small gears into a shallow tin.

overall_soundscape: Steady rain runs down glass over a quiet room tone. Small brass parts click against tin, and a chair creaks as weight shifts.

non_diegetic_music: Sparse piano notes at a slow tempo with long gaps between phrases.""",
]


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class ControlState:
    """Thread-safe live configuration shared by the web UI and producer."""

    def __init__(self, config_path: str, scenes_path: str, refs_dir: str,
                 start_enabled: bool = True, fast_width: int = 448,
                 fast_height: int = 448, music_path: str = "",
                 clip_length: int = 124, playback_fps: float = 14.0):
        self.config_path = config_path
        self.lock = threading.RLock()
        self.enabled = start_enabled
        self.scenes_path = os.path.abspath(os.path.expanduser(scenes_path))
        self.refs_dir = os.path.abspath(os.path.expanduser(refs_dir))
        self.attention = "auto"
        self.ui_language = "en"
        self.scene_play_mode = "random"
        self.scenes_epoch = 0
        self.refs_epoch = 0
        self.clip_length = self._clean_length(clip_length)
        self.playback_fps = float(playback_fps)
        self.continuity_enabled = True
        self.continuity_mode = "guide_only"
        self.continuity_epoch = 0
        self.adaptive_quality = True
        self.fast_width = self._clean_dimension(fast_width)
        self.fast_height = self._clean_dimension(fast_height)
        self.quality_width = 800
        self.quality_height = 800
        self.quality_high_water = 4
        self.quality_low_water = 3
        self.quality_profile_version = 2
        self.external_music_enabled = False
        self.music_path = (os.path.abspath(os.path.expanduser(music_path))
                           if music_path else "")
        self.music_play_mode = "ordered"
        self.music_volume = 0.20
        self.h3_audio_volume = 1.0
        self.music_ducking = False
        self.suppress_generated_music = True
        self.loras: list[dict] = []
        self.manual_prompts: list[dict] = []
        self.runtime = {
            "submitted": 0,
            "completed": 0,
            "inflight": 0,
            "buffered": 0,
            "current_prompt": "",
            "last_generation_s": None,
            "last_error": "",
            "active_attention": "",
            "active_loras": [],
            "continuity_active": False,
            "continuity_picture": None,
            "continuity_frame": "",
            "continuity_mode": self.continuity_mode,
            "story_references": [],
            "quality_mode": "fast",
            "active_resolution": f"{self.fast_width}x{self.fast_height}",
            "viewers": 0,
            "playback_waiting": True,
            "audio_mode": "H3 audio",
            "sources_refreshed": "",
            "active_clip_frames": self.clip_length,
            "active_native_duration_s": self.clip_length / 24.0,
            "active_playback_duration_s": self.clip_length / self.playback_fps,
        }
        self._load()
        self.runtime["active_resolution"] = f"{self.fast_width}x{self.fast_height}"

    @staticmethod
    def _clean_dimension(value) -> int:
        """Clamp H3 dimensions and align them to its 32-pixel canvas."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 448
        value = max(128, min(2048, value))
        return max(128, round(value / 32) * 32)

    @staticmethod
    def _clean_watermark(value, default: int) -> int:
        try:
            return max(0, min(64, int(value)))
        except (TypeError, ValueError):
            return default

    def _load(self) -> None:
        if not os.path.isfile(self.config_path):
            return
        try:
            with open(self.config_path, encoding="utf-8") as f:
                data = json.load(f)
            self.enabled = bool(data.get("enabled", self.enabled))
            self.scenes_path = os.path.abspath(os.path.expanduser(
                str(data.get("scenes_path", self.scenes_path))))
            self.refs_dir = os.path.abspath(os.path.expanduser(
                str(data.get("refs_dir", self.refs_dir))))
            self.attention = str(data.get("attention", "auto"))
            language = str(data.get("ui_language", self.ui_language))
            self.ui_language = language if language in ("en", "de") else "en"
            scene_mode = str(data.get("scene_play_mode", self.scene_play_mode))
            self.scene_play_mode = (scene_mode if scene_mode in ("random", "ordered")
                                    else "random")
            if "clip_length" in data:
                self.clip_length = self._clean_length(data["clip_length"])
            elif "generated_duration_s" in data:
                self.clip_length = self._duration_to_length(data["generated_duration_s"])
            self.continuity_enabled = bool(data.get("continuity_enabled", True))
            mode = str(data.get("continuity_mode", "guide_only"))
            self.continuity_mode = mode if mode in ("guide_only", "dual") else "guide_only"
            self.adaptive_quality = bool(data.get("adaptive_quality", True))
            self.fast_width = self._clean_dimension(data.get("fast_width", self.fast_width))
            self.fast_height = self._clean_dimension(data.get("fast_height", self.fast_height))
            self.quality_width = self._clean_dimension(
                data.get("quality_width", self.quality_width))
            self.quality_height = self._clean_dimension(
                data.get("quality_height", self.quality_height))
            self.quality_high_water = self._clean_watermark(
                data.get("quality_high_water", self.quality_high_water), 4)
            self.quality_low_water = self._clean_watermark(
                data.get("quality_low_water", self.quality_low_water), 3)
            self.external_music_enabled = bool(
                data.get("external_music_enabled", self.external_music_enabled))
            music_path = str(data.get("music_path", self.music_path)).strip()
            self.music_path = (os.path.abspath(os.path.expanduser(music_path))
                               if music_path else "")
            play_mode = str(data.get("music_play_mode", self.music_play_mode))
            self.music_play_mode = (play_mode if play_mode in ("ordered", "random")
                                    else "ordered")
            self.music_volume = self._clean_gain(
                data.get("music_volume", self.music_volume), self.music_volume)
            self.h3_audio_volume = self._clean_gain(
                data.get("h3_audio_volume", self.h3_audio_volume), self.h3_audio_volume)
            self.music_ducking = bool(data.get("music_ducking", self.music_ducking))
            self.suppress_generated_music = bool(
                data.get("suppress_generated_music", self.suppress_generated_music))
            old_profile = int(data.get("quality_profile_version", 1))
            # Migrate only the untouched defaults from the earlier 448/480
            # package. Any values the user customized remain authoritative.
            if (old_profile < 2 and self.fast_width == 448 and self.fast_height == 448
                    and self.quality_width == 480 and self.quality_height == 480
                    and self.quality_high_water == 3 and self.quality_low_water == 1):
                self.quality_width = self.quality_height = 800
                self.quality_high_water, self.quality_low_water = 4, 3
            if self.quality_low_water >= self.quality_high_water:
                self.quality_low_water = max(0, self.quality_high_water - 1)
            self.loras = self._clean_loras(data.get("loras", []))
            prompts = data.get("manual_prompts", [])
            if isinstance(prompts, list):
                for item in prompts:
                    if isinstance(item, dict) and str(item.get("text", "")).strip():
                        self.manual_prompts.append({
                            "id": str(item.get("id") or uuid.uuid4().hex[:10]),
                            "text": str(item["text"]).strip(),
                            "repeat": bool(item.get("repeat", False)),
                        })
        except (OSError, ValueError, TypeError) as exc:
            log(f"control config ignored ({exc})")

    @staticmethod
    def _clean_loras(items) -> list[dict]:
        clean = []
        if not isinstance(items, list):
            return clean
        for item in items[:8]:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", "")).strip()
            try:
                strength = max(-5.0, min(5.0, float(item.get("strength", 1.0))))
            except (TypeError, ValueError):
                strength = 1.0
            clean.append({"name": name, "strength": strength,
                          "enabled": bool(item.get("enabled", True))})
        return clean

    @staticmethod
    def _clean_gain(value, default: float) -> float:
        try:
            return max(0.0, min(2.0, float(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _clean_length(value) -> int:
        """Clamp to H3's valid 17k+5 frame sequence (up to about 15 s)."""
        try:
            value = int(value)
        except (TypeError, ValueError):
            value = 124
        value = max(22, min(362, value))
        return 5 + round((value - 5) / 17) * 17

    @classmethod
    def _duration_to_length(cls, seconds) -> int:
        try:
            target = float(seconds) * 24.0
        except (TypeError, ValueError):
            target = 124
        return cls._clean_length(target)

    def _save_locked(self) -> None:
        data = {
            "enabled": self.enabled,
            "scenes_path": self.scenes_path,
            "refs_dir": self.refs_dir,
            "attention": self.attention,
            "ui_language": self.ui_language,
            "scene_play_mode": self.scene_play_mode,
            "clip_length": self.clip_length,
            "continuity_enabled": self.continuity_enabled,
            "continuity_mode": self.continuity_mode,
            "adaptive_quality": self.adaptive_quality,
            "fast_width": self.fast_width,
            "fast_height": self.fast_height,
            "quality_width": self.quality_width,
            "quality_height": self.quality_height,
            "quality_high_water": self.quality_high_water,
            "quality_low_water": self.quality_low_water,
            "quality_profile_version": self.quality_profile_version,
            "external_music_enabled": self.external_music_enabled,
            "music_path": self.music_path,
            "music_play_mode": self.music_play_mode,
            "music_volume": self.music_volume,
            "h3_audio_volume": self.h3_audio_volume,
            "music_ducking": self.music_ducking,
            "suppress_generated_music": self.suppress_generated_music,
            "loras": self.loras,
            "manual_prompts": self.manual_prompts,
        }
        os.makedirs(os.path.dirname(self.config_path) or ".", exist_ok=True)
        tmp = self.config_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.config_path)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "enabled": self.enabled,
                "scenes_path": self.scenes_path,
                "refs_dir": self.refs_dir,
                "attention": self.attention,
                "ui_language": self.ui_language,
                "scene_play_mode": self.scene_play_mode,
                "scenes_epoch": self.scenes_epoch,
                "refs_epoch": self.refs_epoch,
                "clip_length": self.clip_length,
                "generated_duration_s": self.clip_length / 24.0,
                "playback_fps": self.playback_fps,
                "playback_duration_s": self.clip_length / self.playback_fps,
                "continuity_enabled": self.continuity_enabled,
                "continuity_mode": self.continuity_mode,
                "continuity_epoch": self.continuity_epoch,
                "adaptive_quality": self.adaptive_quality,
                "fast_width": self.fast_width,
                "fast_height": self.fast_height,
                "quality_width": self.quality_width,
                "quality_height": self.quality_height,
                "quality_high_water": self.quality_high_water,
                "quality_low_water": self.quality_low_water,
                "external_music_enabled": self.external_music_enabled,
                "music_path": self.music_path,
                "music_play_mode": self.music_play_mode,
                "music_volume": self.music_volume,
                "h3_audio_volume": self.h3_audio_volume,
                "music_ducking": self.music_ducking,
                "suppress_generated_music": self.suppress_generated_music,
                "loras": copy.deepcopy(self.loras),
                "manual_prompts": copy.deepcopy(self.manual_prompts),
                "runtime": copy.deepcopy(self.runtime),
            }

    def update_settings(self, data: dict) -> None:
        with self.lock:
            was_enabled = self.enabled
            if "enabled" in data:
                self.enabled = bool(data["enabled"])
            if "scenes_path" in data:
                value = str(data["scenes_path"]).strip()
                if value:
                    self.scenes_path = os.path.abspath(os.path.expanduser(value))
            if "refs_dir" in data:
                value = str(data["refs_dir"]).strip()
                if value:
                    self.refs_dir = os.path.abspath(os.path.expanduser(value))
            if "attention" in data:
                self.attention = str(data["attention"]).strip() or "auto"
            if "ui_language" in data:
                value = str(data["ui_language"])
                if value not in ("en", "de"):
                    raise ValueError("Unknown UI language")
                self.ui_language = value
            if "scene_play_mode" in data:
                value = str(data["scene_play_mode"])
                if value not in ("random", "ordered"):
                    raise ValueError("Unknown scene playback mode")
                self.scene_play_mode = value
            if "generated_duration_s" in data:
                self.clip_length = self._duration_to_length(data["generated_duration_s"])
            if "continuity_enabled" in data:
                self.continuity_enabled = bool(data["continuity_enabled"])
            if "continuity_mode" in data:
                mode = str(data["continuity_mode"])
                if mode not in ("guide_only", "dual"):
                    raise ValueError("Unbekannter Fortsetzungsmodus")
                self.continuity_mode = mode
            if "adaptive_quality" in data:
                self.adaptive_quality = bool(data["adaptive_quality"])
            if "fast_width" in data:
                self.fast_width = self._clean_dimension(data["fast_width"])
            if "fast_height" in data:
                self.fast_height = self._clean_dimension(data["fast_height"])
            if "quality_width" in data:
                self.quality_width = self._clean_dimension(data["quality_width"])
            if "quality_height" in data:
                self.quality_height = self._clean_dimension(data["quality_height"])
            if "quality_high_water" in data:
                self.quality_high_water = self._clean_watermark(data["quality_high_water"], 4)
            if "quality_low_water" in data:
                self.quality_low_water = self._clean_watermark(data["quality_low_water"], 3)
            if self.quality_low_water >= self.quality_high_water:
                self.quality_low_water = max(0, self.quality_high_water - 1)
            if "loras" in data:
                self.loras = self._clean_loras(data["loras"])
            if "external_music_enabled" in data:
                self.external_music_enabled = bool(data["external_music_enabled"])
            if "music_path" in data:
                value = str(data["music_path"]).strip()
                self.music_path = (os.path.abspath(os.path.expanduser(value))
                                   if value else "")
            if "music_play_mode" in data:
                value = str(data["music_play_mode"])
                if value not in ("ordered", "random"):
                    raise ValueError("Unbekannter Musik-Wiedergabemodus")
                self.music_play_mode = value
            if "music_volume" in data:
                self.music_volume = self._clean_gain(data["music_volume"], 0.20)
            if "h3_audio_volume" in data:
                self.h3_audio_volume = self._clean_gain(data["h3_audio_volume"], 1.0)
            if "music_ducking" in data:
                self.music_ducking = bool(data["music_ducking"])
            if "suppress_generated_music" in data:
                self.suppress_generated_music = bool(data["suppress_generated_music"])
            self.runtime.update(
                active_clip_frames=self.clip_length,
                active_native_duration_s=self.clip_length / 24.0,
                active_playback_duration_s=self.clip_length / self.playback_fps,
            )
            self._save_locked()
            if self.enabled != was_enabled:
                log("generation RESUMED from control UI" if self.enabled
                    else "generation PAUSED from control UI; already submitted jobs will finish")

    def refresh_sources(self, kind: str) -> None:
        with self.lock:
            if kind == "references":
                self.refs_epoch += 1
            elif kind == "scenes":
                self.scenes_epoch += 1
            else:
                raise ValueError("Unknown source kind")
            self.runtime["sources_refreshed"] = f"{kind}:{time.strftime('%H:%M:%S')}"
        log(f"{kind} refreshed from control UI")

    def reset_continuity(self) -> None:
        """Break the story before the next not-yet-submitted prompt."""
        with self.lock:
            self.continuity_epoch += 1
            self.runtime.update(continuity_active=False,
                                continuity_picture=None,
                                continuity_frame="")
        log("continuity RESET from control UI; next prompt starts a new story")

    def add_prompt(self, text: str, first: bool = False,
                   repeat: bool = False) -> dict:
        item = {"id": uuid.uuid4().hex[:10], "text": text.strip(),
                "repeat": bool(repeat)}
        if not item["text"]:
            raise ValueError("Prompt darf nicht leer sein")
        with self.lock:
            self.manual_prompts.insert(0 if first else len(self.manual_prompts), item)
            self._save_locked()
        return item

    def update_prompt(self, item_id: str, text: str,
                      repeat: bool | None = None) -> None:
        if not text.strip():
            raise ValueError("Prompt darf nicht leer sein")
        with self.lock:
            item = next((p for p in self.manual_prompts if p["id"] == item_id), None)
            if item is None:
                raise KeyError(item_id)
            item["text"] = text.strip()
            if repeat is not None:
                item["repeat"] = bool(repeat)
            self._save_locked()

    def delete_prompt(self, item_id: str) -> None:
        with self.lock:
            before = len(self.manual_prompts)
            self.manual_prompts = [p for p in self.manual_prompts if p["id"] != item_id]
            if len(self.manual_prompts) == before:
                raise KeyError(item_id)
            self._save_locked()

    def move_prompt(self, item_id: str, delta: int) -> None:
        with self.lock:
            pos = next((i for i, p in enumerate(self.manual_prompts)
                        if p["id"] == item_id), None)
            if pos is None:
                raise KeyError(item_id)
            dest = max(0, min(len(self.manual_prompts) - 1, pos + delta))
            self.manual_prompts.insert(dest, self.manual_prompts.pop(pos))
            self._save_locked()

    def take_manual_prompt(self) -> dict | None:
        with self.lock:
            if not self.manual_prompts:
                return None
            item = self.manual_prompts.pop(0)
            # A repeated item rotates to the end. Other queued prompts still
            # get a turn; once it is the only item, it loops indefinitely.
            if item.get("repeat"):
                self.manual_prompts.append(item)
            self._save_locked()
            return copy.deepcopy(item)

    def requeue_manual_prompt(self, item: dict | None) -> None:
        if item is None:
            return
        with self.lock:
            # A repeating item may already have rotated to the end. Move it
            # back to the front on submit failure so retries preserve order.
            self.manual_prompts = [p for p in self.manual_prompts
                                   if p["id"] != item["id"]]
            self.manual_prompts.insert(0, item)
            self._save_locked()

    def set_runtime(self, **values) -> None:
        with self.lock:
            self.runtime.update(values)


CONTROL_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>H3 Ref2V Continuity Control</title>
<style>
:root{color-scheme:dark;--bg:#0a0d12;--card:#121722;--line:#283143;--ink:#eef3ff;--muted:#9eabc0;--blue:#6aa8ff;--green:#53d69b;--red:#ff6b78}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 20% 0,#17233b 0,#0a0d12 35%);font:15px system-ui;color:var(--ink)}
main{max-width:1180px;margin:auto;padding:24px}.top{display:flex;gap:16px;justify-content:space-between;align-items:center;margin-bottom:18px}
h1{font-size:24px;margin:0}.sub,.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.card{background:rgba(18,23,34,.94);border:1px solid var(--line);border-radius:14px;padding:18px;box-shadow:0 12px 40px #0005}.wide{grid-column:1/-1}
h2{font-size:16px;margin:0 0 14px}label{display:block;color:var(--muted);font-size:12px;margin:10px 0 6px}input,select,textarea,button{font:inherit}input,select,textarea{width:100%;background:#090d14;border:1px solid var(--line);border-radius:8px;color:var(--ink);padding:10px}textarea{min-height:150px;resize:vertical}button{border:0;border-radius:9px;padding:10px 14px;background:#26334a;color:var(--ink);cursor:pointer}button:hover{filter:brightness(1.15)}button.primary{background:#246bd6}.on{background:#147d54}.off,.danger{background:#8d2934}.row{display:flex;gap:9px;align-items:center}.row>*{flex:1}.row button{flex:0 0 auto}.stats{display:grid;grid-template-columns:repeat(5,1fr);gap:8px}.stat{background:#090d14;padding:12px;border-radius:9px}.stat b{display:block;font-size:19px;margin-top:4px}.pill{padding:6px 10px;border-radius:99px;background:#26334a}.lora{display:grid;grid-template-columns:28px 1fr 100px 42px;gap:8px;align-items:center;margin:8px 0}.prompt{border:1px solid var(--line);border-radius:10px;padding:10px;margin:9px 0}.prompt textarea{min-height:95px}.actions{display:flex;gap:7px;margin-top:8px;flex-wrap:wrap}.notice{border-left:3px solid var(--blue);padding-left:10px;color:var(--muted);font-size:13px}.error{color:var(--red);white-space:pre-wrap}.ok{color:var(--green)}
@media(max-width:780px){.grid{grid-template-columns:1fr}.wide{grid-column:auto}.stats{grid-template-columns:1fr 1fr}.top{align-items:flex-start;flex-direction:column}.lora{grid-template-columns:28px 1fr 80px 42px}}
</style></head><body><main>
<div class="top"><div><h1>H3 Ref2V Continuity</h1><div class="sub" data-i18n="subtitle">Short clips with real character references and continuous stories.</div></div><div class="row"><select id="uiLanguage" onchange="changeLanguage(this.value)"><option value="en">English</option><option value="de">Deutsch</option></select><button id="toggle">Loading…</button></div></div>
<div class="grid">
 <section class="card wide"><h2 data-i18n="status">Status</h2><div class="stats">
  <div class="stat"><span data-i18n="submitted">Submitted</span><b id="submitted">–</b></div><div class="stat"><span data-i18n="completed">Completed</span><b id="completed">–</b></div>
  <div class="stat"><span data-i18n="inComfy">In ComfyUI</span><b id="inflight">–</b></div><div class="stat"><span data-i18n="buffer">Video buffer</span><b id="buffered">–</b></div><div class="stat"><span data-i18n="viewers">Viewers</span><b id="viewers">–</b></div>
 </div><p id="viewerInfo" class="notice"></p><p id="current" class="muted"></p><div id="runtimeError" class="error"></div></section>
 <section class="card"><h2 data-i18n="sources">Sources & Attention</h2>
  <label for="scenes">prompts_scenes.txt</label><input id="scenes">
  <label for="sceneMode" data-i18n="sceneOrder">Scene playback</label><select id="sceneMode"><option value="random" data-i18n="random">Random</option><option value="ordered" data-i18n="ordered">In order</option></select>
  <div class="actions"><button onclick="refreshScenes()" data-i18n="refreshPrompts">Refresh prompts</button></div>
  <label for="refs" data-i18n="refsFolder">character_refs folder</label><input id="refs">
  <div class="actions"><button onclick="refreshReferences()" data-i18n="refreshRefs">Refresh references</button></div>
  <p id="pathInfo" class="notice"></p>
  <label for="attention" data-i18n="attentionBackend">Attention backend</label><select id="attention"></select>
  <p id="attentionInfo" class="notice"></p><button class="primary" onclick="saveSettings()" data-i18n="saveSettings">Save settings</button>
 </section>
 <section class="card"><h2 data-i18n="continuityTitle">Continuation</h2>
  <label><input id="continuity" type="checkbox" style="width:auto;margin-right:8px"><span data-i18n="useLastFrame">Use the final frame for the next clip</span></label>
  <label for="continuityMode" data-i18n="continuityMode">Continuation mode</label><select id="continuityMode"><option value="guide_only" data-i18n="guideOnly">Guide only – more stable colors</option><option value="dual" data-i18n="dualMode">Guide + Picture – strongest binding</option></select>
  <p id="continuityInfo" class="notice"></p>
  <div class="actions"><button class="primary" onclick="saveSettings()" data-i18n="apply">Apply</button><button class="danger" onclick="resetContinuity()" data-i18n="resetStory">New story / Reset</button></div>
  <p class="notice" data-i18n="continuityNote">Continuation submits one prompt at a time because the next graph needs the previous final frame.</p>
 </section>
 <section class="card"><h2 data-i18n="timingTitle">Duration & adaptive quality</h2>
  <label for="generatedDuration" data-i18n="generatedDuration">Native generation duration (seconds)</label><input id="generatedDuration" type="number" min="0.92" max="15.08" step="0.01">
  <p id="durationInfo" class="notice"></p>
  <label><input id="adaptiveQuality" type="checkbox" style="width:auto;margin-right:8px"><span data-i18n="adaptive">Choose resolution from video buffer</span></label>
  <div class="row"><div><label for="fastWidth" data-i18n="fastWidth">Fast width</label><input id="fastWidth" type="number" min="128" max="2048" step="32"></div><div><label for="fastHeight" data-i18n="fastHeight">Fast height</label><input id="fastHeight" type="number" min="128" max="2048" step="32"></div></div>
  <div class="row"><div><label for="qualityWidth" data-i18n="hqWidth">HQ width</label><input id="qualityWidth" type="number" min="128" max="2048" step="32"></div><div><label for="qualityHeight" data-i18n="hqHeight">HQ height</label><input id="qualityHeight" type="number" min="128" max="2048" step="32"></div></div>
  <div class="row"><div><label for="qualityHigh" data-i18n="hqOn">Enable HQ at buffer</label><input id="qualityHigh" type="number" min="1" max="64" step="1"></div><div><label for="qualityLow" data-i18n="hqOff">Disable HQ at buffer</label><input id="qualityLow" type="number" min="0" max="63" step="1"></div></div>
  <p id="qualityInfo" class="notice"></p><button class="primary" onclick="saveSettings()" data-i18n="applyQuality">Apply duration & quality</button>
 </section>
 <section class="card"><h2 data-i18n="audioTitle">Audio & music bed</h2>
  <label><input id="externalMusic" type="checkbox" style="width:auto;margin-right:8px"><span data-i18n="mixMusic">Mix continuous external music</span></label>
  <label for="musicPath" data-i18n="musicSource">Music file or folder</label><input id="musicPath" placeholder="%USERPROFILE%\Music or %USERPROFILE%\Music\soundtrack.mp3">
  <label for="musicPlayMode" data-i18n="musicOrder">Folder playback</label><select id="musicPlayMode"><option value="ordered" data-i18n="ordered">In order</option><option value="random" data-i18n="random">Random</option></select>
  <div class="row"><div><label for="musicVolume" data-i18n="musicVolume">Music volume</label><input id="musicVolume" type="number" min="0" max="2" step="0.05"></div><div><label for="h3Volume" data-i18n="h3Volume">H3 audio volume</label><input id="h3Volume" type="number" min="0" max="2" step="0.05"></div></div>
  <label><input id="musicDucking" type="checkbox" style="width:auto;margin-right:8px"><span data-i18n="ducking">Lower music while H3 audio is active</span></label>
  <label><input id="suppressMusic" type="checkbox" style="width:auto;margin-right:8px"><span data-i18n="suppressMusic">Suppress H3 music with non_diegetic_music: N/A</span></label>
  <p id="audioInfo" class="notice"></p><button class="primary" onclick="saveSettings()" data-i18n="applyAudio">Apply audio</button>
  <p class="notice" data-i18n="audioNote">Files continue across clip boundaries; a folder advances after each full track.</p>
 </section>
 <section class="card"><h2 data-i18n="lorasTitle">LoRAs</h2><div id="loras"></div><div class="actions"><button onclick="addLora()" data-i18n="addLora">+ LoRA</button><button class="primary" onclick="saveSettings()" data-i18n="applyLoras">Apply LoRAs</button></div>
  <p class="notice" data-i18n="loraNote">Up to eight model-only LoRAs. Disabled entries and strength zero are skipped.</p></section>
 <section class="card"><h2 data-i18n="insertPrompt">Insert prompt</h2><textarea id="newPrompt" placeholder="Full H3 prompt or a short scene description …"></textarea>
  <label><input id="newPromptRepeat" type="checkbox" style="width:auto;margin-right:8px"><span data-i18n="repeatPrompt">Repeat this prompt</span></label>
  <div class="actions"><button class="primary" onclick="addPrompt(true)" data-i18n="insertNext">Insert next</button><button onclick="addPrompt(false)" data-i18n="append">Append</button></div>
  <p class="notice" data-i18n="repeatNote">Repeat prompts rotate to the back of the manual queue.</p></section>
 <section class="card"><h2 data-i18n="manualQueue">Manual prompt queue</h2><div id="promptQueue" class="muted">Empty</div></section>
</div></main>
<script>
let state=null, loraOptions=[];
const I18N={
 en:{subtitle:'Short clips with real character references and continuous stories.',status:'Status',submitted:'Submitted',completed:'Completed',inComfy:'In ComfyUI',buffer:'Video buffer',viewers:'Viewers',sources:'Sources & Attention',sceneOrder:'Scene playback',random:'Random',ordered:'In order',refreshPrompts:'Refresh prompts',refsFolder:'character_refs folder',refreshRefs:'Refresh references',attentionBackend:'Attention backend',saveSettings:'Save settings',continuityTitle:'Continuation',useLastFrame:'Use the final frame for the next clip',continuityMode:'Continuation mode',guideOnly:'Guide only – more stable colors',dualMode:'Guide + Picture – strongest binding',apply:'Apply',resetStory:'New story / Reset',continuityNote:'Continuation submits one prompt at a time because the next graph needs the previous final frame.',timingTitle:'Duration & adaptive quality',generatedDuration:'Native generation duration (seconds)',adaptive:'Choose resolution from video buffer',fastWidth:'Fast width',fastHeight:'Fast height',hqWidth:'HQ width',hqHeight:'HQ height',hqOn:'Enable HQ at buffer',hqOff:'Disable HQ at buffer',applyQuality:'Apply duration & quality',audioTitle:'Audio & music bed',mixMusic:'Mix continuous external music',musicSource:'Music file or folder',musicOrder:'Folder playback',musicVolume:'Music volume',h3Volume:'H3 audio volume',ducking:'Lower music while H3 audio is active',suppressMusic:'Suppress H3 music with non_diegetic_music: N/A',applyAudio:'Apply audio',audioNote:'Files continue across clip boundaries; a folder advances after each full track.',lorasTitle:'LoRAs',addLora:'+ LoRA',applyLoras:'Apply LoRAs',loraNote:'Up to eight model-only LoRAs. Disabled entries and strength zero are skipped.',insertPrompt:'Insert prompt',repeatPrompt:'Repeat this prompt',repeat:'Repeat',insertNext:'Insert next',append:'Append',repeatNote:'Repeat prompts rotate to the back of the manual queue.',manualQueue:'Manual prompt queue',emptyQueue:'Empty – scene file is used.',selectLora:'— select —',save:'Save',delete:'Delete',noLora:'No live LoRA.',viewerWaiting:'No viewer – playback is paused while the buffer fills.',viewerActive:'Stream is being watched – playback is running.',current:'Current',lastClip:'last clip',generationOn:'Generation: ON',generationOff:'Generation: OFF',sceneFound:'scene file found',sceneMissing:'scene file MISSING',scenes:'scenes',references:'references',active:'Active',available:'available',notYet:'not determined yet',guideActive:'Active as frame-0 guide',dualActive:'Active as guide + Picture',identityAnchors:'identity anchors',newStory:'No continuation frame – the next clip starts a new story.',hq:'HQ',fast:'Fast',switchAt:'switch at buffer',frames:'frames',native:'native',playback:'playback',at:'at',tracks:'tracks',musicMissing:'music source EMPTY/MISSING'},
 de:{subtitle:'Kurze Clips mit echten Charakterreferenzen und fortlaufender Geschichte.',status:'Status',submitted:'Eingereicht',completed:'Fertig',inComfy:'In ComfyUI',buffer:'Videopuffer',viewers:'Zuschauer',sources:'Quellen & Attention',sceneOrder:'Szenenwiedergabe',random:'Zufällig',ordered:'In Reihenfolge',refreshPrompts:'Prompts aktualisieren',refsFolder:'character_refs Ordner',refreshRefs:'Referenzen aktualisieren',attentionBackend:'Attention-Backend',saveSettings:'Einstellungen speichern',continuityTitle:'Fortsetzung',useLastFrame:'Letzten Frame für den nächsten Clip verwenden',continuityMode:'Fortsetzungsmodus',guideOnly:'Nur Guide – stabilere Farben',dualMode:'Guide + Picture – stärkste Bildbindung',apply:'Übernehmen',resetStory:'Neue Geschichte / Reset',continuityNote:'Im Fortsetzungsmodus wird jeweils ein Prompt eingereicht, da der nächste Graph den letzten Frame benötigt.',timingTitle:'Dauer & adaptive Qualität',generatedDuration:'Native Generierungsdauer (Sekunden)',adaptive:'Auflösung automatisch nach Videopuffer wählen',fastWidth:'Schnell Breite',fastHeight:'Schnell Höhe',hqWidth:'HQ Breite',hqHeight:'HQ Höhe',hqOn:'HQ an ab Puffer',hqOff:'HQ aus bei Puffer',applyQuality:'Dauer & Qualität übernehmen',audioTitle:'Audio & Musikbett',mixMusic:'Durchgehende externe Musik zumischen',musicSource:'Musikdatei oder Musikordner',musicOrder:'Ordner-Wiedergabe',musicVolume:'Musiklautstärke',h3Volume:'H3-Tonlautstärke',ducking:'Musik bei H3-Ton automatisch absenken',suppressMusic:'H3-Musik mit non_diegetic_music: N/A unterdrücken',applyAudio:'Audio übernehmen',audioNote:'Dateien laufen über Clipgrenzen weiter; im Ordner wird nach einem vollständigen Titel gewechselt.',lorasTitle:'LoRAs',addLora:'+ LoRA',applyLoras:'LoRAs übernehmen',loraNote:'Bis zu acht Model-only-LoRAs. Deaktivierte Einträge und Stärke null werden übersprungen.',insertPrompt:'Prompt einschieben',repeatPrompt:'Diesen Prompt wiederholen',repeat:'Wiederholen',insertNext:'Als nächsten einfügen',append:'Ans Ende',repeatNote:'Repeat-Prompts rotieren ans Ende der manuellen Warteschlange.',manualQueue:'Manuelle Prompt-Warteschlange',emptyQueue:'Leer – die Szenendatei wird verwendet.',selectLora:'— auswählen —',save:'Speichern',delete:'Löschen',noLora:'Keine Live-LoRA.',viewerWaiting:'Kein Zuschauer – Wiedergabe pausiert, der Puffer wird aufgebaut.',viewerActive:'Stream wird angesehen – Wiedergabe läuft.',current:'Aktuell',lastClip:'letzter Clip',generationOn:'Generation: AN',generationOff:'Generation: AUS',sceneFound:'Szenendatei gefunden',sceneMissing:'Szenendatei FEHLT',scenes:'Szenen',references:'Referenzen',active:'Aktiv',available:'verfügbar',notYet:'noch nicht ermittelt',guideActive:'Aktiv als Frame-0-Guide',dualActive:'Aktiv als Guide + Picture',identityAnchors:'Identitätsanker',newStory:'Noch kein Anschlussbild – der nächste Clip beginnt eine neue Geschichte.',hq:'HQ',fast:'Schnell',switchAt:'Umschalten bei Puffer',frames:'Frames',native:'nativ',playback:'Wiedergabe',at:'bei',tracks:'Titel',musicMissing:'Musikquelle LEER/FEHLT'}
};
function tr(k){let lang=(state&&state.ui_language)||'en';return (I18N[lang]&&I18N[lang][k])||I18N.en[k]||k}
function applyTranslations(){document.documentElement.lang=(state&&state.ui_language)||'en';document.querySelectorAll('[data-i18n]').forEach(e=>{e.textContent=tr(e.dataset.i18n)});let n=document.querySelector('#newPrompt');if(n)n.placeholder=(state?.ui_language==='de'?'Vollständiger H3-Prompt oder kurze Szenenbeschreibung …':'Full H3 prompt or a short scene description …')}
async function api(path, body){let o={};if(body!==undefined)o={method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};let r=await fetch(path,o);let j=await r.json();if(!r.ok)throw Error(j.error||r.statusText);return j}
function esc(s){return String(s??'').replace(/[&<>\"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;'}[c]))}
function loraSelect(selected){return `<option value="">${tr('selectLora')}</option>`+loraOptions.map(x=>`<option ${x===selected?'selected':''} value="${esc(x)}">${esc(x)}</option>`).join('')}
function render(){applyTranslations();let r=state.runtime||{};document.querySelector('#uiLanguage').value=state.ui_language||'en';document.querySelector('#submitted').textContent=r.submitted??0;document.querySelector('#completed').textContent=r.completed??0;document.querySelector('#inflight').textContent=r.inflight??0;document.querySelector('#buffered').textContent=r.buffered??0;document.querySelector('#viewers').textContent=r.viewers??'–';document.querySelector('#viewerInfo').textContent=r.playback_waiting?tr('viewerWaiting'):tr('viewerActive');document.querySelector('#current').textContent=r.current_prompt?tr('current')+': '+r.current_prompt:'';if(r.last_generation_s!=null)document.querySelector('#current').textContent+=(document.querySelector('#current').textContent?' · ':'')+tr('lastClip')+' '+Number(r.last_generation_s).toFixed(1)+' s';document.querySelector('#runtimeError').textContent=r.last_error||'';
 let active=(r.active_loras||[]).map(x=>x.name+' @ '+x.strength).join(', ');if(active)document.querySelector('#current').textContent+=(document.querySelector('#current').textContent?' · ':'')+'LoRA: '+active;
 let t=document.querySelector('#toggle');t.textContent=state.enabled?tr('generationOn'):tr('generationOff');t.className=state.enabled?'on':'off';
 let settingIds=['scenes','sceneMode','refs','attention','continuity','continuityMode','generatedDuration','adaptiveQuality','fastWidth','fastHeight','qualityWidth','qualityHeight','qualityHigh','qualityLow','externalMusic','musicPath','musicPlayMode','musicVolume','h3Volume','musicDucking','suppressMusic'];let editingSettings=document.activeElement&&(document.activeElement.closest('#loras')||settingIds.includes(document.activeElement.id));if(!editingSettings){document.querySelector('#scenes').value=state.scenes_path;document.querySelector('#sceneMode').value=state.scene_play_mode||'random';document.querySelector('#refs').value=state.refs_dir;document.querySelector('#continuity').checked=!!state.continuity_enabled;document.querySelector('#continuityMode').value=state.continuity_mode||'guide_only';document.querySelector('#generatedDuration').value=Number(state.generated_duration_s).toFixed(2);document.querySelector('#adaptiveQuality').checked=!!state.adaptive_quality;document.querySelector('#fastWidth').value=state.fast_width;document.querySelector('#fastHeight').value=state.fast_height;document.querySelector('#qualityWidth').value=state.quality_width;document.querySelector('#qualityHeight').value=state.quality_height;document.querySelector('#qualityHigh').value=state.quality_high_water;document.querySelector('#qualityLow').value=state.quality_low_water;document.querySelector('#externalMusic').checked=!!state.external_music_enabled;document.querySelector('#musicPath').value=state.music_path||'';document.querySelector('#musicPlayMode').value=state.music_play_mode||'ordered';document.querySelector('#musicVolume').value=state.music_volume;document.querySelector('#h3Volume').value=state.h3_audio_volume;document.querySelector('#musicDucking').checked=!!state.music_ducking;document.querySelector('#suppressMusic').checked=!!state.suppress_generated_music;let att=document.querySelector('#attention');let opts=['auto',...(state.attention_options||[])];att.innerHTML=[...new Set(opts)].map(x=>`<option ${x===state.attention?'selected':''}>${esc(x)}</option>`).join('');renderLoras()}
 let aliases=state.reference_aliases||[];document.querySelector('#pathInfo').textContent=(state.paths?.scenes_exists?tr('sceneFound'):tr('sceneMissing'))+' · '+(state.scene_count||0)+' '+tr('scenes')+' · '+aliases.length+' '+tr('references')+(aliases.length?': '+aliases.join(', '):'');
 document.querySelector('#attentionInfo').textContent=tr('active')+': '+(r.active_attention||tr('notYet'))+' · '+tr('available')+': '+(state.attention_options||[]).join(', ');
 let storyRefs=(r.story_references||[]).join(', ');document.querySelector('#continuityInfo').textContent=r.continuity_active?(((r.continuity_mode||state.continuity_mode)==='guide_only'?tr('guideActive'):(tr('dualActive')+' '+r.continuity_picture))+ ' · '+(r.continuity_frame||'')+(storyRefs?' · '+tr('identityAnchors')+': '+storyRefs:'')):tr('newStory');
 document.querySelector('#durationInfo').textContent=state.clip_length+' '+tr('frames')+' · '+tr('native')+' '+Number(state.generated_duration_s).toFixed(2)+' s · '+tr('playback')+' '+Number(state.playback_duration_s).toFixed(2)+' s '+tr('at')+' '+state.playback_fps+' fps';
 document.querySelector('#qualityInfo').textContent=tr('active')+': '+(r.quality_mode==='quality'?tr('hq'):tr('fast'))+' · '+(r.active_resolution||'–')+' · '+tr('switchAt')+' '+state.quality_low_water+'/'+state.quality_high_water;
 document.querySelector('#audioInfo').textContent=(r.audio_mode||'H3 audio')+' · '+(state.music_tracks||[]).length+' '+tr('tracks')+(state.external_music_enabled&&!state.paths?.music_exists?' · '+tr('musicMissing'):'');
 if(!(document.activeElement&&document.activeElement.closest('#promptQueue')))renderPrompts();}
function renderLoras(){let root=document.querySelector('#loras');root.innerHTML='';(state.loras||[]).forEach((x,i)=>{let d=document.createElement('div');d.className='lora';d.innerHTML=`<input type="checkbox" data-k="enabled" data-i="${i}" ${x.enabled?'checked':''}><select data-k="name" data-i="${i}">${loraSelect(x.name)}</select><input type="number" step="0.05" min="-5" max="5" value="${x.strength}" data-k="strength" data-i="${i}"><button class="danger" onclick="removeLora(${i})">×</button>`;root.appendChild(d)});if(!state.loras?.length)root.innerHTML='<div class="muted">'+tr('noLora')+'</div>'}
function readLoras(){return [...document.querySelectorAll('#loras .lora')].map(d=>({enabled:d.querySelector('[data-k=enabled]').checked,name:d.querySelector('[data-k=name]').value,strength:Number(d.querySelector('[data-k=strength]').value)}))}
function addLora(){state.loras=readLoras();if(state.loras.length<8)state.loras.push({enabled:true,name:'',strength:0.6});renderLoras()}
function removeLora(i){state.loras=readLoras();state.loras.splice(i,1);renderLoras()}
function renderPrompts(){let q=document.querySelector('#promptQueue'),items=state.manual_prompts||[];q.innerHTML='';if(!items.length){q.textContent=tr('emptyQueue');return}items.forEach((p,i)=>{let d=document.createElement('div');d.className='prompt';d.innerHTML=`<textarea id="p_${p.id}">${esc(p.text)}</textarea><label><input id="repeat_${p.id}" type="checkbox" style="width:auto;margin-right:8px" ${p.repeat?'checked':''}> ${tr('repeat')}</label><div class="actions"><button onclick="movePrompt('${p.id}',-1)" ${i===0?'disabled':''}>↑</button><button onclick="movePrompt('${p.id}',1)" ${i===items.length-1?'disabled':''}>↓</button><button class="primary" onclick="editPrompt('${p.id}')">${tr('save')}</button><button class="danger" onclick="deletePrompt('${p.id}')">${tr('delete')}</button></div>`;q.appendChild(d)})}
async function refresh(){try{state=await api('/api/state');loraOptions=state.lora_options||[];render()}catch(e){document.querySelector('#runtimeError').textContent=e.message}}
async function saveSettings(){try{await api('/api/settings',{ui_language:document.querySelector('#uiLanguage').value,scenes_path:document.querySelector('#scenes').value,scene_play_mode:document.querySelector('#sceneMode').value,refs_dir:document.querySelector('#refs').value,attention:document.querySelector('#attention').value,continuity_enabled:document.querySelector('#continuity').checked,continuity_mode:document.querySelector('#continuityMode').value,generated_duration_s:Number(document.querySelector('#generatedDuration').value),adaptive_quality:document.querySelector('#adaptiveQuality').checked,fast_width:Number(document.querySelector('#fastWidth').value),fast_height:Number(document.querySelector('#fastHeight').value),quality_width:Number(document.querySelector('#qualityWidth').value),quality_height:Number(document.querySelector('#qualityHeight').value),quality_high_water:Number(document.querySelector('#qualityHigh').value),quality_low_water:Number(document.querySelector('#qualityLow').value),external_music_enabled:document.querySelector('#externalMusic').checked,music_path:document.querySelector('#musicPath').value,music_play_mode:document.querySelector('#musicPlayMode').value,music_volume:Number(document.querySelector('#musicVolume').value),h3_audio_volume:Number(document.querySelector('#h3Volume').value),music_ducking:document.querySelector('#musicDucking').checked,suppress_generated_music:document.querySelector('#suppressMusic').checked,loras:readLoras()});await refresh()}catch(e){alert(e.message)}}
async function changeLanguage(lang){try{await api('/api/settings',{ui_language:lang});state.ui_language=lang;render()}catch(e){alert(e.message)}}
async function refreshReferences(){try{await api('/api/settings',{refs_dir:document.querySelector('#refs').value});await api('/api/references/refresh',{});await refresh()}catch(e){alert(e.message)}}
async function refreshScenes(){try{await api('/api/settings',{scenes_path:document.querySelector('#scenes').value,scene_play_mode:document.querySelector('#sceneMode').value});await api('/api/scenes/refresh',{});await refresh()}catch(e){alert(e.message)}}
document.querySelector('#toggle').onclick=async()=>{await api('/api/settings',{enabled:!state.enabled});await refresh()};
async function resetContinuity(){try{await api('/api/continuity/reset',{});await refresh()}catch(e){alert(e.message)}}
async function addPrompt(first){let e=document.querySelector('#newPrompt'),rep=document.querySelector('#newPromptRepeat');try{await api('/api/prompt/add',{text:e.value,first,repeat:rep.checked});e.value='';rep.checked=false;await refresh()}catch(x){alert(x.message)}}
async function editPrompt(id){await api('/api/prompt/update',{id,text:document.querySelector('#p_'+id).value,repeat:document.querySelector('#repeat_'+id).checked});document.activeElement.blur();await refresh()}
async function deletePrompt(id){await api('/api/prompt/delete',{id});document.activeElement.blur();await refresh()}
async function movePrompt(id,delta){await api('/api/prompt/move',{id,delta});document.activeElement.blur();await refresh()}
refresh();setInterval(refresh,2000);
</script></body></html>"""


class ControlServer:
    """Small dependency-free local web UI for live stream controls."""

    def __init__(self, host: str, port: int, state: ControlState, metadata):
        control = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def _send(self, status: int, payload, content_type="application/json; charset=utf-8"):
                raw = (payload.encode("utf-8") if isinstance(payload, str)
                       else json.dumps(payload, ensure_ascii=False).encode("utf-8"))
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def _body(self) -> dict:
                length = min(int(self.headers.get("Content-Length", "0")), 2_000_000)
                return json.loads(self.rfile.read(length) or b"{}")

            def do_GET(self):
                if self.path == "/":
                    self._send(200, CONTROL_HTML, "text/html; charset=utf-8")
                    return
                if self.path == "/api/state":
                    data = control.state.snapshot()
                    data.update(control.metadata())
                    self._send(200, data)
                    return
                self._send(404, {"error": "Nicht gefunden"})

            def do_POST(self):
                try:
                    data = self._body()
                    if self.path == "/api/settings":
                        control.state.update_settings(data)
                    elif self.path == "/api/prompt/add":
                        control.state.add_prompt(str(data.get("text", "")),
                                                 bool(data.get("first")),
                                                 bool(data.get("repeat")))
                    elif self.path == "/api/prompt/update":
                        control.state.update_prompt(str(data.get("id", "")),
                                                    str(data.get("text", "")),
                                                    (bool(data["repeat"])
                                                     if "repeat" in data else None))
                    elif self.path == "/api/prompt/delete":
                        control.state.delete_prompt(str(data.get("id", "")))
                    elif self.path == "/api/prompt/move":
                        control.state.move_prompt(str(data.get("id", "")), int(data.get("delta", 0)))
                    elif self.path == "/api/continuity/reset":
                        control.state.reset_continuity()
                    elif self.path == "/api/references/refresh":
                        control.state.refresh_sources("references")
                    elif self.path == "/api/scenes/refresh":
                        control.state.refresh_sources("scenes")
                    else:
                        self._send(404, {"error": "Nicht gefunden"})
                        return
                    self._send(200, {"ok": True})
                except (ValueError, TypeError, KeyError, OSError) as exc:
                    self._send(400, {"error": str(exc)})

            def log_message(self, *args):
                pass

        self.state = state
        self.metadata = metadata
        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


class PromptPool:
    """Random scene x random character for the original text-only placeholders."""

    def __init__(self, scenes_path: str, characters_path: str, curated_share: float):
        self.scenes_path = scenes_path
        self.curated_share = curated_share
        pools = json.load(open(characters_path, encoding="utf-8"))
        self.curated = pools["curated"]
        self.full = pools["full"]
        self._scene_signature = None
        self._scene_cursor = 0
        log(f"prompt pool: {len(self.scenes())} scenes x "
            f"{len(self.full)} characters ({len(self.curated)} curated, "
            f"{curated_share:.0%} of draws)")

    def scenes(self, scenes_path: str | None = None) -> list[str]:
        # re-read every draw so the file can be edited while the stream runs
        text = open(scenes_path or self.scenes_path, encoding="utf-8").read()
        sep = "\n---\n"
        return [b.strip() for b in text.split(sep) if b.strip()]

    def _pick(self) -> str:
        pool = self.curated if (self.curated and random.random() < self.curated_share) else self.full
        return random.choice(pool)

    def fill(self, scene: str) -> tuple[str, str]:
        """Fill any {NAME...} placeholders in an arbitrary scene."""
        slots = ["{NAME}"] + [f"{{NAME{i}}}" for i in range(2, 10)]
        slots = [s for s in slots if s in scene]

        picked: list[str] = []
        for _ in slots:
            name = self._pick()
            for _ in range(20):
                if name not in picked:
                    break
                name = self._pick()
            picked.append(name)

        for slot, name in sorted(zip(slots, picked), key=lambda p: -len(p[0])):
            scene = scene.replace(slot, name)
        return scene, " + ".join(picked)

    def draw(self, scenes_path: str | None = None, mode: str = "random",
             refresh_epoch: int = 0) -> tuple[str, str, int]:
        """Fill {NAME} placeholders, then return the finished scene text."""
        scenes = self.scenes(scenes_path)
        if not scenes:
            raise ValueError(f"no scenes found in {scenes_path or self.scenes_path}")
        if mode == "ordered":
            signature = (os.path.abspath(scenes_path or self.scenes_path),
                         tuple(scenes), int(refresh_epoch))
            if signature != self._scene_signature:
                self._scene_signature = signature
                self._scene_cursor = 0
            idx = self._scene_cursor % len(scenes)
            self._scene_cursor += 1
        else:
            idx = random.randrange(len(scenes))
        scene, who = self.fill(scenes[idx])
        return scene, who, idx


def normalize_manual_prompt(text: str) -> str:
    """Accept either a full H3 prompt or a convenient one-field description."""
    text = text.strip()
    if "integrated_multimodal_description:" in text.casefold():
        return text
    return ("integrated_multimodal_description:\n" + text
            + "\n\noverall_soundscape:\nNatural diegetic sound matching the scene."
            + "\n\nnon_diegetic_music:\nN/A")


def apply_music_prompt_policy(text: str, suppress: bool) -> str:
    """Force H3's non-diegetic music field off without touching scene audio.

    The external music bed is mixed after generation. Keeping H3's own music
    as well would layer two unrelated keys and rhythms, which sounds worse than
    either source alone. Diegetic dialogue, ambience and effects in
    ``overall_soundscape`` remain unchanged.
    """
    if not suppress:
        return text
    section = re.compile(
        r"(^\s*non_diegetic_music\s*:\s*)(.*?)(?=^\s*[a-z][a-z0-9_ ]*\s*:|\Z)",
        re.IGNORECASE | re.MULTILINE | re.DOTALL,
    )
    if section.search(text):
        return section.sub(lambda match: match.group(1) + "N/A\n", text, count=1).rstrip()
    return text.rstrip() + "\n\nnon_diegetic_music:\nN/A"


def trim_prompt_to_duration(text: str, duration_s: float) -> str:
    """Drop authored shots whose start timestamp lies outside this clip.

    The repository scene pool was authored for 362 frames and typically puts
    Shot 2 around 7-10 seconds. A 124-frame job ends after about 5.17 seconds;
    keeping those instructions wastes prompt capacity and encourages a rushed
    or ignored cut. Timestamps are strictly increasing, so the first out-of-
    range shot and everything after it can be removed from the visual section.
    """
    section = re.search(r"\n\s*overall_soundscape\s*:", text, re.IGNORECASE)
    visual_end = section.start() if section else len(text)
    visual = text[:visual_end]
    pattern = re.compile(
        r"\[Shot\s+\d+\]\s+At\s+(\d\d):(\d\d)\.(\d\d\d)",
        re.IGNORECASE,
    )
    for match in pattern.finditer(visual):
        seconds = (int(match.group(1)) * 60 + int(match.group(2))
                   + int(match.group(3)) / 1000.0)
        if seconds >= duration_s:
            visual = visual[:match.start()].rstrip()
            suffix = text[visual_end:].lstrip()
            return visual + ("\n\n" + suffix if suffix else "")
    return text


def _combo_values(object_info: dict, node_type: str, field: str) -> list[str]:
    spec = object_info.get(node_type, {}).get("input", {})
    all_inputs = {**spec.get("required", {}), **spec.get("optional", {})}
    value = all_inputs.get(field, [])
    if isinstance(value, (list, tuple)) and value and isinstance(value[0], list):
        return [str(x) for x in value[0]]
    return []


def available_attention_backends(object_info: dict) -> list[str]:
    values = _combo_values(object_info, "MiniMaxH3BlockAttentionSplit", "middle_backend")
    return values or ["pytorch attention"]


def choose_attention(preferred: str, available: list[str]) -> str:
    """Resolve auto/Sage preference without ever submitting an invalid enum."""
    if preferred in available:
        return preferred
    for needle in ("sage", "comfy kitchen", "pytorch"):
        hit = next((name for name in available if needle in name.casefold()), None)
        if hit:
            return hit
    return available[0]


def set_attention_backend(prompt: dict, object_info: dict,
                          preferred: str) -> tuple[dict, str]:
    ids = [k for k, v in prompt.items()
           if v.get("class_type") == "MiniMaxH3BlockAttentionSplit"]
    if "MiniMaxH3BlockAttentionSplit" not in object_info:
        return prompt, "workflow default"
    selected = choose_attention(preferred, available_attention_backends(object_info))
    out = {k: {**v, "inputs": dict(v.get("inputs", {}))} for k, v in prompt.items()}
    if not ids:
        # The stock Ref2V template has no attention patch. Insert it directly
        # before BasicGuider so the selected model already includes the bundled
        # Ref2V Turbo LoRA and any live LoRAs.
        guiders = [k for k, v in out.items() if v.get("class_type") == "BasicGuider"]
        if len(guiders) != 1:
            return prompt, "workflow default"
        guider_id = guiders[0]
        model_link = out[guider_id]["inputs"].get("model")
        if not isinstance(model_link, list):
            return prompt, "workflow default"
        available = available_attention_backends(object_info)
        edge = choose_attention("pytorch attention", available)
        out["live_attention"] = {
            "class_type": "MiniMaxH3BlockAttentionSplit",
            "inputs": {
                "model": model_link,
                "edge_backend": edge,
                "middle_backend": selected,
                "head_pct": 0.0,
                "tail_pct": 0.0,
            },
        }
        out[guider_id]["inputs"]["model"] = ["live_attention", 0]
    elif len(ids) == 1:
        out[ids[0]]["inputs"]["middle_backend"] = selected
        out[ids[0]]["inputs"]["head_pct"] = 0.0
        out[ids[0]]["inputs"]["tail_pct"] = 0.0
    else:
        return prompt, "workflow default"
    return out, selected


def apply_loras(prompt: dict, object_info: dict,
                specs: list[dict]) -> tuple[dict, list[dict]]:
    """Insert a model-only LoRA chain directly after the single UNET loader."""
    active = [x for x in specs if x.get("enabled") and x.get("name")
              and abs(float(x.get("strength", 0.0))) > 1e-9]
    if not active:
        return prompt, []
    if "LoraLoaderModelOnly" not in object_info:
        raise RuntimeError("ComfyUI bietet LoraLoaderModelOnly nicht an")
    allowed = set(_combo_values(object_info, "LoraLoaderModelOnly", "lora_name"))
    for item in active:
        if allowed and item["name"] not in allowed:
            raise RuntimeError(f"LoRA nicht in ComfyUI gefunden: {item['name']}")

    out = {k: {**v, "inputs": copy.deepcopy(v.get("inputs", {}))}
           for k, v in prompt.items()}
    loaders = [k for k, v in out.items() if v.get("class_type") == "UNETLoader"]
    if len(loaders) != 1:
        raise RuntimeError(f"genau ein UNETLoader erwartet, gefunden: {len(loaders)}")
    loader_id = loaders[0]
    final_id = f"live_lora_{len(active) - 1}"

    # Redirect only the original consumers. The new chain itself is added
    # afterwards so it cannot accidentally be redirected into a cycle.
    for node in out.values():
        for key, value in list(node["inputs"].items()):
            if value == [loader_id, 0]:
                node["inputs"][key] = [final_id, 0]

    previous = loader_id
    used = []
    for index, item in enumerate(active):
        node_id = f"live_lora_{index}"
        out[node_id] = {
            "class_type": "LoraLoaderModelOnly",
            "inputs": {
                "model": [previous, 0],
                "lora_name": item["name"],
                "strength_model": float(item["strength"]),
            },
        }
        previous = node_id
        used.append({"name": item["name"], "strength": float(item["strength"])})
    return out, used


def set_video_vae(prompt: dict, name: str | None) -> dict:
    """Point the VIDEO VAELoader at ``name``, leaving the audio one alone.

    Both VAEs load through the same node type, so a class-level override would
    silently retarget the audio VAE too; pick the loader whose current filename
    identifies it.
    """
    if not name:
        return prompt
    hit = [k for k, v in prompt.items() if v["class_type"] == "VAELoader"
           and "video" in v["inputs"].get("vae_name", "").lower()]
    if len(hit) != 1:
        raise SystemExit(f"expected exactly one video VAELoader, found {len(hit)}")
    out = dict(prompt)
    out[hit[0]] = {**out[hit[0]],
                   "inputs": {**out[hit[0]]["inputs"], "vae_name": name}}
    return out


def set_vae_tiling(prompt: dict, tile_size: int) -> dict:
    """Insert H3VideoVaeTiling between the video VAELoader and VAEDecode.

    The H3 video VAE hardcodes 256 px spatial tiles, and decode time tracks the
    tile count rather than the pixels. Enlarging the tile is therefore tempting
    and wrong -- the decoder is a ViT that attends within a tile, so a bigger
    one is out of distribution and the picture degrades badly. This exists to
    pin 256 explicitly, because the node mutates the VAE in place and the
    setting outlives the prompt: ``tile_size <= 0`` inherits whatever was set
    last, which is not the same as restoring the default.
    """
    if tile_size <= 0:
        return prompt
    dec = next((k for k, v in prompt.items() if v["class_type"] == "VAEDecode"), None)
    if dec is None:
        raise SystemExit("no VAEDecode node to retile")
    out = dict(prompt)
    out["vae_tiling"] = {"class_type": "H3VideoVaeTiling",
                         "inputs": {"vae": out[dec]["inputs"]["vae"],
                                    "tiling": True, "tile_size": tile_size}}
    out[dec] = {**out[dec], "inputs": {**out[dec]["inputs"], "vae": ["vae_tiling", 0]}}
    return out


def swap_writer(prompt: dict, writer: str, prefix: str) -> dict:
    """Rewrite CreateVideo -> SaveVideo into a single faster writer node.

    Done on the API prompt rather than in the workflow file so the workflow
    stays the one that is shipped and opened in the UI; the substitution is a
    property of how the stream writes clips, not of the graph.
    """
    if writer not in WRITERS:
        return prompt
    node_type, params = WRITERS[writer]
    save = next((k for k, v in prompt.items() if v["class_type"] == "SaveVideo"), None)
    if save is None:
        raise SystemExit("no SaveVideo node to replace; --writer needs the stock workflow")
    create_id = prompt[save]["inputs"]["video"][0]
    create = prompt[create_id]
    if create["class_type"] != "CreateVideo":
        raise SystemExit(f"SaveVideo is fed by {create['class_type']}, not CreateVideo")

    fps = float(create["inputs"].get("fps", 24.0))
    if node_type == "H3FastWriteVideo":
        inputs = {"images": create["inputs"]["images"], "fps": fps,
                  "filename_prefix": prefix}
    else:
        inputs = {"images": create["inputs"]["images"], "frame_rate": fps,
                  "loop_count": 0, "filename_prefix": prefix,
                  "pingpong": False, "save_output": True}
    if "audio" in create["inputs"]:
        inputs["audio"] = create["inputs"]["audio"]
    inputs.update(params)

    out = {k: v for k, v in prompt.items() if k not in (save, create_id)}
    out["clip_writer"] = {"class_type": node_type, "inputs": inputs}
    return out


def scan_character_references(refs_dir: str) -> dict[str, tuple[str, str]]:
    """Build alias -> (display name, image path) from filenames in character_refs."""
    os.makedirs(refs_dir, exist_ok=True)
    allowed = {".png", ".jpg", ".jpeg", ".webp"}
    refs: dict[str, tuple[str, str]] = {}
    for filename in sorted(os.listdir(refs_dir), key=str.casefold):
        path = os.path.join(refs_dir, filename)
        if not os.path.isfile(path):
            continue
        stem, ext = os.path.splitext(filename)
        if not stem or ext.casefold() not in allowed:
            continue
        key = stem.casefold()
        if key in refs:
            raise ValueError(
                f"duplicate character alias {stem!r} in {refs_dir!r}; "
                "filenames must be unique ignoring case"
            )
        refs[key] = (stem, path)
    return refs


def find_references_in_prompt(text: str, refs: dict[str, tuple[str, str]]) -> list[tuple[str, str]]:
    """Return referenced characters in first-mention order, matching whole aliases."""
    hits: list[tuple[int, str, str]] = []
    for _key, (alias, path) in refs.items():
        # Whole-name matching avoids e.g. 'ben' matching 'bench'.
        pattern = rf"(?<!\w){re.escape(alias)}(?!\w)"
        m = re.search(pattern, text, flags=re.IGNORECASE)
        if m:
            hits.append((m.start(), alias, path))
    hits.sort(key=lambda item: item[0])
    return [(alias, path) for _pos, alias, path in hits[:9]]


def annotate_reference_prompt(text: str, refs: list[tuple[str, str]], mode: str) -> str:
    """Bind character names to H3 reference tags without rewriting the scene itself."""
    out = text
    if mode == "r2v":
        # H3's stock R2V node addresses images as <Picture 1>, <Picture 2>, ...
        for i, (alias, _path) in enumerate(refs, 1):
            pattern = re.compile(rf"(?<!\w){re.escape(alias)}(?!\w)", re.IGNORECASE)
            out = pattern.sub(lambda m, i=i: f"{m.group(0)} <Picture {i}>", out, count=1)
        return out

    if mode == "first_frame" and refs:
        alias, _path = refs[0]
        pattern = re.compile(rf"(?<!\w){re.escape(alias)}(?!\w)", re.IGNORECASE)
        return pattern.sub(
            lambda m: f"{m.group(0)} (the person shown in the first-frame reference image)",
            out, count=1,
        )
    return out


def bind_generated_cast_to_continuity(text: str, cast: str) -> str:
    """Replace randomly filled template names with roles from the last frame.

    Ready-made scenes use celebrity names only to fill ``{NAME}`` slots. Once
    a story is already running those fresh identities conflict with the last
    frame reference, so automatic scenes instead address the continuing cast.
    Explicit names written directly into a scene are left untouched.
    """
    names = [name.strip() for name in cast.split(" + ") if name.strip()]
    if not names:
        return text
    soundscape = re.search(r"\n\s*overall_soundscape\s*:", text, re.IGNORECASE)
    visual = text if soundscape is None else text[:soundscape.start()]
    tail = "" if soundscape is None else text[soundscape.start():]
    for index, name in enumerate(names, 1):
        if len(names) == 1:
            role = f"the same continuing person shown in {CONTINUITY_CAST_TOKEN}"
        else:
            role = (f"continuing character {index} shown in "
                    f"{CONTINUITY_CAST_TOKEN}")
        visual = re.sub(rf"(?<!\w){re.escape(name)}(?!\w)", role, visual,
                        flags=re.IGNORECASE)
    return visual + tail


def prepend_visual_instruction(text: str, instruction: str) -> str:
    """Place an instruction before Shot 1, where long prompts cannot bury it."""
    header = re.search(r"integrated_multimodal_description\s*:\s*",
                       text, flags=re.IGNORECASE)
    if header:
        return text[:header.end()] + instruction + " " + text[header.end():]
    marker = re.search(r"\n\s*overall_soundscape\s*:", text, flags=re.IGNORECASE)
    if marker:
        return (text[:marker.start()].rstrip() + "\n" + instruction + "\n\n"
                + text[marker.start():].lstrip())
    return text.rstrip() + "\n\n" + instruction


def add_continuity_instruction(text: str, mode: str,
                               picture: int | None = None) -> str:
    """Describe either stable guide-only or legacy dual continuity."""
    if mode == "dual":
        if picture is None:
            raise ValueError("dual continuity requires a picture number")
        instruction = CONTINUITY_SENTENCE.format(picture=picture)
    else:
        instruction = CONTINUITY_GUIDE_SENTENCE
    return prepend_visual_instruction(text, instruction)


def add_identity_anchor_instruction(text: str, count: int) -> str:
    """Tell H3 how carried character refs relate to an automatic scene."""
    if count <= 0:
        return text
    tags = ", ".join(f"<Picture {i}>" for i in range(1, count + 1))
    noun = "this identity anchor" if count == 1 else "these identity anchors"
    instruction = (f"Keep the continuing characters identical to {tags}; use "
                   f"{noun} for faces, hair, body, and clothing throughout.")
    return prepend_visual_instruction(text, instruction)


def _copy_ref_to_comfy(source_path: str, comfy_input: str) -> tuple[str, str]:
    source_abs = os.path.abspath(source_path)
    input_abs = os.path.abspath(comfy_input)
    try:
        if os.path.commonpath([source_abs, input_abs]) == input_abs:
            relative = os.path.relpath(source_abs, input_abs).replace(os.sep, "/")
            return relative, source_abs
    except ValueError:
        pass  # different Windows drives: copy into ComfyUI's input tree below
    subdir = "fasth3_refs"
    dest_dir = os.path.join(comfy_input, subdir)
    os.makedirs(dest_dir, exist_ok=True)
    dest_name = os.path.basename(source_path)
    dest_path = os.path.join(dest_dir, dest_name)
    if source_abs != os.path.abspath(dest_path):
        # Avoid needless disk writes when the source did not change.
        if (not os.path.exists(dest_path) or
                os.path.getsize(dest_path) != os.path.getsize(source_path) or
                os.path.getmtime(dest_path) < os.path.getmtime(source_path)):
            shutil.copy2(source_path, dest_path)
    return f"{subdir}/{dest_name}", dest_path


def attach_reference_images(prompt: dict, refs: list[tuple[str, str]],
                            comfy_input: str, mode: str,
                            continuity_picture: int | None = None,
                            continuity_guide_path: str | None = None,
                            ref_image_size: str = "match") -> dict:
    """Wire matched character images into the active H3 conditioning node."""
    out = {k: {**v, "inputs": dict(v.get("inputs", {}))} for k, v in prompt.items()}

    if mode == "r2v":
        hit = [k for k, v in out.items() if v.get("class_type") == "MiniMaxH3ReferenceToVideo"]
        if len(hit) != 1:
            raise SystemExit(f"expected exactly one MiniMaxH3ReferenceToVideo node, found {len(hit)}")
        node_id = hit[0]
        # Replace ComfyUI's Autogrow node with fixed optional sockets. Some
        # ComfyUI releases accept API-form Autogrow dictionaries but silently
        # discard them. H3ReferenceToVideoFixed avoids that failure mode.
        out[node_id]["class_type"] = "H3ReferenceToVideoFixed"
        for key in list(out[node_id]["inputs"]):
            if (key == "ref_images" or key.startswith("ref_images.")
                    or key.startswith("ref_image_")):
                del out[node_id]["inputs"][key]
        # UI-to-API conversion can omit unlinked combo widgets. This input is
        # required by the fixed node, so always write the live CLI value.
        out[node_id]["inputs"]["ref_image_size"] = ref_image_size

        load_ids: list[str] = []
        for i, (_alias, source_path) in enumerate(refs[:9], 1):
            image_key, _ = _copy_ref_to_comfy(source_path, comfy_input)
            load_id = f"character_ref_image_{i}"
            out[load_id] = {"class_type": "LoadImage", "inputs": {"image": image_key}}
            out[node_id]["inputs"][f"ref_image_{i}"] = [load_id, 0]
            load_ids.append(load_id)

        if continuity_guide_path is not None:
            if continuity_picture is not None:
                if not (1 <= continuity_picture <= len(load_ids)):
                    raise RuntimeError("invalid continuity picture index")
                guide_load_id = load_ids[continuity_picture - 1]
            else:
                image_key, _ = _copy_ref_to_comfy(continuity_guide_path, comfy_input)
                guide_load_id = "continuity_guide_image"
                out[guide_load_id] = {
                    "class_type": "LoadImage", "inputs": {"image": image_key}}
            guiders = [k for k, v in out.items() if v.get("class_type") == "BasicGuider"]
            if len(guiders) != 1:
                raise RuntimeError(f"exactly one BasicGuider expected, found {len(guiders)}")
            guide_id = "continuity_first_frame_guide"
            out[guide_id] = {
                "class_type": "MiniMaxH3AddGuide",
                "inputs": {
                    "positive": [node_id, 0],
                    "latent": [node_id, 1],
                    "vae": out[node_id]["inputs"]["vae"],
                    "image": [guide_load_id, 0],
                    "frame_idx": 0,
                },
            }
            out[guiders[0]]["inputs"]["conditioning"] = [guide_id, 0]
        return out

    if not refs:
        return prompt

    # FastH3 FL2VA fallback: only one image can be supplied and it becomes frame 0.
    hit = [k for k, v in out.items() if v.get("class_type") == "MiniMaxH3ImageToVideo"]
    if len(hit) != 1:
        raise SystemExit(f"expected exactly one MiniMaxH3ImageToVideo node, found {len(hit)}")
    alias, source_path = refs[0]
    image_key, _ = _copy_ref_to_comfy(source_path, comfy_input)
    load_id = "character_ref_image_0"
    out[load_id] = {"class_type": "LoadImage", "inputs": {"image": image_key}}
    node_id = hit[0]
    out[node_id]["inputs"]["first_frame"] = [load_id, 0]
    return out


class Producer(threading.Thread):
    """Generate clips forever and hand their paths to the feeder."""

    def __init__(self, args, out_q: queue.Queue, stop: threading.Event,
                 pool: PromptPool, control: ControlState):
        super().__init__(daemon=True)
        self.args, self.q, self.stop, self.pool, self.control = args, out_q, stop, pool, control
        self.workflow = json.load(open(args.workflow, encoding="utf-8"))
        self.object_info = get("/object_info")
        writer_node = WRITERS.get(args.writer, (None, {}))[0]
        if writer_node and writer_node not in self.object_info:
            raise SystemExit(
                f"ComfyUI node missing for --writer {args.writer!r}: {writer_node}. "
                "Run start_h3_r2v_continuity.ps1 once to install the bundled/upstream "
                "custom nodes, restart ComfyUI, and start it again."
            )
        initial = control.snapshot()
        refs = scan_character_references(initial["refs_dir"])
        aliases = [display for display, _path in refs.values()]
        log(f"character refs: {len(aliases)} found in {initial['refs_dir']}")
        if aliases:
            log("  aliases: " + ", ".join(aliases))

        ui_types = {n.get("type") for n in self.workflow.get("nodes", []) if isinstance(n, dict)}
        if "MiniMaxH3ReferenceToVideo" in ui_types:
            self.reference_mode = "r2v"
            log("reference mode: MiniMaxH3ReferenceToVideo (<Picture N> tags, up to 9 images)")
        else:
            self.reference_mode = "first_frame"
            log("reference mode: FastH3 first-frame fallback (one matched character per scene)")
        if self.reference_mode == "r2v":
            missing = [name for name in ("H3ReferenceToVideoFixed", "MiniMaxH3AddGuide")
                       if name not in self.object_info]
            if missing:
                raise SystemExit(
                    "ComfyUI node(s) missing: " + ", ".join(missing)
                    + ". Install custom_nodes/h3_r2v_fixed and restart ComfyUI."
                )
        self.check_models()
        self.n = 0
        self.submitted = 0
        self.last_done = time.time()
        self.continuity_path: str | None = None
        self.story_refs: list[tuple[str, str]] = []
        self.continuity_epoch_seen = initial["continuity_epoch"]
        self.quality_mode = False
        self.active_resolution: str | None = None

    def metadata(self) -> dict:
        """Live choices and path diagnostics for the web UI."""
        snap = self.control.snapshot()
        try:
            refs = scan_character_references(snap["refs_dir"])
            ref_aliases = [display for display, _path in refs.values()]
            refs_error = ""
        except (OSError, ValueError) as exc:
            ref_aliases, refs_error = [], str(exc)
        try:
            scene_count = len(self.pool.scenes(snap["scenes_path"]))
            scenes_error = ""
        except (OSError, ValueError) as exc:
            scene_count, scenes_error = 0, str(exc)
        tracks = music_files(snap["music_path"])
        return {
            "lora_options": _combo_values(self.object_info, "LoraLoaderModelOnly", "lora_name"),
            "attention_options": available_attention_backends(self.object_info),
            "reference_aliases": ref_aliases,
            "scene_count": scene_count,
            "music_tracks": [os.path.basename(path) for path in tracks],
            "paths": {
                "scenes_exists": os.path.isfile(snap["scenes_path"]),
                "refs_exists": os.path.isdir(snap["refs_dir"]),
                "refs_error": refs_error,
                "scenes_error": scenes_error,
                "music_exists": bool(tracks),
                "music_is_directory": os.path.isdir(snap["music_path"]),
            },
        }

    def check_models(self) -> None:
        """Fail at startup, not mid-prompt, on a model ComfyUI cannot see.

        The video VAE in particular defaults to a file this repository tells you
        to build rather than one you download, so a fresh checkout will not have
        it. ComfyUI's own error for that arrives as a rejected prompt, several
        seconds in and wrapped in validation JSON.
        """
        combos = {
            "--video-vae": (self.args.video_vae, "VAELoader", "vae_name"),
            "--dit": (self.args.dit, "UNETLoader", "unet_name"),
            "--clip": (self.args.clip, "CLIPLoader", "clip_name"),
        }
        for flag, (want, node, field) in combos.items():
            if not want:
                continue
            spec = self.object_info.get(node, {}).get("input", {}).get("required", {})
            have = spec.get(field, [None])[0]
            if isinstance(have, list) and want not in have:
                lines = [f"{flag}: ComfyUI does not list {want!r} for {node}.",
                         f"  available: {', '.join(map(str, have[:8]))}"
                         + (" ..." if len(have) > 8 else "")]
                if flag == "--video-vae":
                    lines.append("  the default video VAE is built rather than downloaded"
                                 " -- see the README, or pass one you already have.")
                raise SystemExit("\n".join(lines))

    def choose_resolution(self, settings: dict) -> tuple[int, int, str]:
        """Select fast/HQ dimensions using buffer hysteresis.

        A separate high and low watermark prevents the graph from alternating
        resolution on every clip when the queue sits on a boundary.
        """
        depth = self.q.qsize()
        if not settings["adaptive_quality"]:
            self.quality_mode = False
        elif self.quality_mode:
            if depth <= settings["quality_low_water"]:
                self.quality_mode = False
        elif depth >= settings["quality_high_water"]:
            self.quality_mode = True

        if self.quality_mode:
            width, height, mode = (settings["quality_width"],
                                   settings["quality_height"], "quality")
        else:
            width, height, mode = settings["fast_width"], settings["fast_height"], "fast"
        resolution = f"{width}x{height}"
        marker = f"{mode}:{resolution}"
        if marker != self.active_resolution:
            label = "HQ" if mode == "quality" else "FAST"
            reason = (f"buffer={depth}, thresholds "
                      f"{settings['quality_low_water']}/{settings['quality_high_water']}")
            if not settings["adaptive_quality"]:
                reason = "adaptive disabled"
            log(f"adaptive quality: {label} {resolution} ({reason})")
            self.active_resolution = marker
        self.control.set_runtime(quality_mode=mode, active_resolution=resolution)
        return width, height, mode

    def build(self, prompt_text: str, seed: int, refs: list[tuple[str, str]],
              settings: dict, width: int, height: int,
              continuity_picture: int | None = None,
              continuity_guide_path: str | None = None,
              ) -> tuple[dict, str, list[dict]]:
        cond_node = ("MiniMaxH3ReferenceToVideo"
                     if self.reference_mode == "r2v" else "MiniMaxH3ImageToVideo")
        ov = {
            cond_node: {"prompt": prompt_text, "width": width,
                        "height": height, "length": settings["clip_length"]},
            "RandomNoise": {"noise_seed": seed},
            "SaveVideo": {"filename_prefix": SCRATCH_PREFIX},
        }
        if self.reference_mode == "r2v":
            ov[cond_node]["ref_image_size"] = self.args.ref_image_size
        if self.args.dit:
            ov["UNETLoader"] = {"unet_name": self.args.dit}
        if self.args.clip:
            ov["CLIPLoader"] = {"clip_name": self.args.clip}
        prompt = to_api(self.workflow, self.object_info, ov)
        prompt = attach_reference_images(prompt, refs, self.args.comfy_input,
                                         self.reference_mode, continuity_picture,
                                         continuity_guide_path,
                                         self.args.ref_image_size)
        prompt = set_video_vae(prompt, self.args.video_vae)
        prompt = set_vae_tiling(prompt, self.args.vae_tile)
        prompt, attention = set_attention_backend(
            prompt, self.object_info, settings.get("attention", "auto"))
        prompt, active_loras = apply_loras(prompt, self.object_info, settings.get("loras", []))
        return swap_writer(prompt, self.args.writer, SCRATCH_PREFIX), attention, active_loras

    def next_seed(self) -> int:
        """One seed per clip, walking up from --seed.

        Off the SUBMIT counter and not the completion counter: with several
        prompts in flight the two differ, and a reused seed would silently ship
        a duplicate clip. A walk rather than a random draw because it never
        collides, and the scene and character are drawn randomly anyway -- the
        clip a seed lands on is different every run regardless.
        """
        return (self.args.seed + self.submitted) & 0x7FFFFFFF

    def _clear_continuity(self) -> None:
        old, self.continuity_path = self.continuity_path, None
        self.story_refs = []
        if old:
            try:
                os.remove(old)
            except OSError:
                pass
        self.control.set_runtime(continuity_active=False,
                                 continuity_picture=None,
                                 continuity_frame="", story_references=[])

    def _capture_continuity(self, clip_path: str, job_epoch: int) -> None:
        settings = self.control.snapshot()
        if (not settings["continuity_enabled"]
                or settings["continuity_epoch"] != job_epoch):
            self._clear_continuity()
            return
        dest_dir = os.path.join(self.args.comfy_input, CONTINUITY_SUBDIR)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, f"last_frame_{self.n:06d}.png")
        cmd = [self.args.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
               "-sseof", "-0.08", "-i", clip_path, "-map", "0:v:0",
               "-frames:v", "1", dest]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, text=True)
        if result.returncode or not os.path.isfile(dest):
            log("continuity frame extraction failed: "
                + (result.stderr.strip()[-300:] or f"ffmpeg exit {result.returncode}"))
            self._clear_continuity()
            return
        old, self.continuity_path = self.continuity_path, dest
        if old and old != dest:
            try:
                os.remove(old)
            except OSError:
                pass
        self.control.set_runtime(continuity_active=True,
                                 continuity_picture=None,
                                 continuity_frame=os.path.basename(dest),
                                 continuity_mode=settings.get("continuity_mode", "guide_only"),
                                 story_references=[alias for alias, _path in self.story_refs])

    def submit(self) -> tuple[str, str, int, int, int, int] | None:
        settings = self.control.snapshot()
        if settings["continuity_epoch"] != self.continuity_epoch_seen:
            self._clear_continuity()
            self.continuity_epoch_seen = settings["continuity_epoch"]
        if not settings["continuity_enabled"] and self.continuity_path:
            self._clear_continuity()
        manual = self.control.take_manual_prompt()
        try:
            if manual:
                text, filled_who = self.pool.fill(normalize_manual_prompt(manual["text"]))
                idx = -1
                who = ("manual repeat" if manual.get("repeat") else "manual")
                if filled_who:
                    who += " · " + filled_who
            else:
                text, who, idx = self.pool.draw(
                    settings["scenes_path"], settings["scene_play_mode"],
                    settings["scenes_epoch"])
            character_refs = scan_character_references(settings["refs_dir"])
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self.control.requeue_manual_prompt(manual)
            self.control.set_runtime(last_error=f"Prompt-Quelle: {exc}")
            log(f"prompt source error: {exc}")
            return None
        clip_length = settings["clip_length"]
        text = trim_prompt_to_duration(text, clip_length / 24.0)
        text = apply_music_prompt_policy(text, settings["suppress_generated_music"])
        if (manual is None and self.reference_mode == "r2v"
                and settings["continuity_enabled"] and self.continuity_path):
            text = bind_generated_cast_to_continuity(text, who)
            who = "auto continuation"
        # Explicit names replace the story's identity anchors. If a continuing
        # scene names nobody, retain the last successful clip's character refs
        # so automatic templates cannot slowly lose the original faces.
        explicit_refs = find_references_in_prompt(text, character_refs)
        continuity_active = (self.reference_mode == "r2v"
                             and settings["continuity_enabled"]
                             and self.continuity_path is not None)
        carried_refs = False
        if explicit_refs:
            refs = explicit_refs
            next_story_refs = list(explicit_refs)
        elif continuity_active and self.story_refs:
            refs = [(alias, path) for alias, path in self.story_refs
                    if os.path.isfile(path)]
            next_story_refs = list(refs)
            carried_refs = bool(refs)
        else:
            refs = []
            next_story_refs = []
        if self.reference_mode == "first_frame" and len(refs) > 1:
            log("scene mentions multiple reference characters; FastH3 first-frame mode "
                f"can send only one, using {refs[0][0]!r}")
            refs = refs[:1]
        continuity_mode = settings.get("continuity_mode", "guide_only")
        continuity_picture = None
        continuity_guide_path = self.continuity_path if continuity_active else None
        if continuity_active and continuity_mode == "dual":
            # Legacy strongest-binding mode reserves the ninth Ref2V slot for
            # the same image that is also attached as the frame-0 guide.
            refs = refs[:8]
            continuity_picture = len(refs) + 1
        elif self.reference_mode == "r2v":
            refs = refs[:9]
        next_story_refs = list(refs)
        prompt_text = annotate_reference_prompt(text, refs, self.reference_mode)
        ref_names = [alias for alias, _path in refs]
        if carried_refs:
            prompt_text = add_identity_anchor_instruction(prompt_text, len(refs))
        if continuity_active:
            if continuity_mode == "dual":
                refs.append(("previous clip", self.continuity_path))
                cast_reference = f"<Picture {continuity_picture}>"
            else:
                cast_reference = "the supplied first-frame guide"
            prompt_text = prompt_text.replace(CONTINUITY_CAST_TOKEN, cast_reference)
            prompt_text = add_continuity_instruction(
                prompt_text, continuity_mode, continuity_picture)
            self.control.set_runtime(continuity_active=True,
                                     continuity_picture=continuity_picture,
                                     continuity_frame=os.path.basename(self.continuity_path),
                                     continuity_mode=continuity_mode,
                                     story_references=ref_names)
        if ref_names:
            who = (who + "  " if who else "") + "refs=" + "+".join(ref_names)

        seed = self.next_seed()
        try:
            render_width, render_height, quality_mode = self.choose_resolution(settings)
            graph, attention, active_loras = self.build(
                prompt_text, seed, refs, settings, render_width, render_height,
                continuity_picture, continuity_guide_path)
            pid = post("/prompt", {"prompt": graph,
                                   "client_id": "fasth3-stream",
                                   "extra_data": VHS_EXTRA_DATA})["prompt_id"]
        except urllib.error.HTTPError as exc:
            self.control.requeue_manual_prompt(manual)
            message = exc.read().decode("utf-8", "replace")[:500]
            self.control.set_runtime(last_error=message)
            log(f"submit rejected: {message[:300]}")
            return None
        except (RuntimeError, OSError, ValueError) as exc:
            self.control.requeue_manual_prompt(manual)
            self.control.set_runtime(last_error=str(exc))
            log(f"submit rejected: {exc}")
            return None
        self.submitted += 1
        self.story_refs = next_story_refs
        quality_label = "HQ" if quality_mode == "quality" else "fast"
        who = ((who + "  ") if who else "") + f"{quality_label}={render_width}x{render_height}"
        who += f"  frames={clip_length}"
        self.control.set_runtime(
            submitted=self.submitted,
            current_prompt=prompt_text[:280].replace("\n", " "),
            last_error="",
            active_attention=attention,
            active_loras=active_loras,
            continuity_mode=continuity_mode,
            story_references=[alias for alias, _path in self.story_refs],
        )
        return pid, who, idx, seed, settings["continuity_epoch"], clip_length

    def collect(self, pid: str) -> str | None:
        """Block until ``pid`` leaves the queue, then return its clip path."""
        path = None
        while not self.stop.is_set():
            hist = get(f"/history/{pid}")
            if pid in hist:
                for node_out in hist[pid].get("outputs", {}).values():
                    for items in node_out.values():
                        if not isinstance(items, list):
                            continue
                        for it in items:
                            if isinstance(it, dict) and it.get("filename", "").endswith(".mp4"):
                                path = os.path.join(self.args.comfy_output, it.get("subfolder", ""),
                                                    it["filename"])
                return self.await_file(path) if path else None
            time.sleep(0.25)
        return None

    def await_file(self, path: str) -> str | None:
        """Wait for an asynchronously written clip to be renamed into place.

        With ``--writer h3fast`` the node returns as soon as it has handed the
        frames to its encoder thread, so /history reports the clip a moment
        before it exists. The node writes to ``<name>.part`` and renames, so
        the name appearing is the completion signal -- and because the rename
        is atomic, seeing it means the whole clip is there.
        """
        deadline = time.time() + self.args.write_timeout
        while not self.stop.is_set():
            if os.path.exists(path):
                return path
            if time.time() > deadline:
                log(f"clip never appeared within {self.args.write_timeout:g}s: "
                    f"{os.path.basename(path)}")
                return None
            time.sleep(0.02)
        return None

    def run(self) -> None:
        inflight: list[tuple[str, str, int, int, int, int]] = []
        was_enabled = self.control.snapshot()["enabled"]
        if not was_enabled:
            log("generation starts PAUSED; enable it in the control UI")
        while not self.stop.is_set():
            enabled = self.control.snapshot()["enabled"]
            if enabled and not was_enabled:
                # Completion intervals are used as throughput samples. Never
                # count the user's paused time as GPU generation time.
                self.last_done = time.time()
                self.control.set_runtime(last_generation_s=None, last_error="")
            was_enabled = enabled
            # Top up ComfyUI's queue first. Blocking on q.put below is the
            # backpressure that bounds this: it stalls the top-up, and the
            # overshoot is at most --pipeline clips.
            target_pipeline = (1 if self.control.snapshot()["continuity_enabled"]
                               else self.args.pipeline)
            while (len(inflight) < target_pipeline and not self.stop.is_set()
                   and self.control.snapshot()["enabled"]):
                job = self.submit()
                if job is None:
                    time.sleep(3)
                    break
                inflight.append(job)
                self.control.set_runtime(inflight=len(inflight), buffered=self.q.qsize())
            if not inflight:
                self.control.set_runtime(inflight=0, buffered=self.q.qsize())
                time.sleep(0.2)
                continue

            pid, who, idx, seed, job_epoch, clip_length = inflight.pop(0)
            path = self.collect(pid)
            if path and os.path.exists(path):
                self.n += 1
                now = time.time()
                interval, self.last_done = now - self.last_done, now
                # names last and untruncated: the cast is drawn from an unseeded
                # random, so scene + seed alone do not reproduce a clip -- the
                # names are the rest of the recipe, and a five-hander needs all
                # of them
                log(f"clip {self.n:04d}  scene {idx:03d}  seed {seed:<10d} "
                    f"{interval:5.1f}s  queue={self.q.qsize() + 1}  {who}")
                self._capture_continuity(path, job_epoch)
                # the interval between completions, not the job's own wall time:
                # with a pipeline the latter includes waiting behind its
                # predecessor and would overstate the cost per clip roughly
                # --pipeline-fold
                self.q.put((path, interval, idx, seed, clip_length))
                self.control.set_runtime(completed=self.n, inflight=len(inflight),
                                         buffered=self.q.qsize(), last_generation_s=interval)
            elif not self.stop.is_set():
                log("generation produced no file; retrying")
                time.sleep(2)


class Broadcast:
    """Fan one paced MPEG-TS stream out to any number of HTTP clients.

    ffmpeg's own ``-listen 1`` HTTP output serves exactly one client and then
    DIES: the moment that client disconnects the muxer aborts (-10053) and the
    port is gone for good, so a single premature connection attempt kills the
    stream permanently. Pacing still belongs to ffmpeg (``-re``), but the socket
    does not -- this owns it so viewers can come and go.
    """

    def __init__(self, host: str, port: int, control: ControlState):
        self.clients: set[queue.Queue] = set()
        self.lock = threading.Lock()
        self.viewer_event = threading.Event()
        self.control = control
        broadcast = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                q: queue.Queue = queue.Queue(maxsize=256)
                with broadcast.lock:
                    broadcast.clients.add(q)
                    viewers = len(broadcast.clients)
                    broadcast.viewer_event.set()
                broadcast.control.set_runtime(viewers=viewers,
                                              playback_waiting=False)
                log(f"viewer connected ({viewers} now)")
                try:
                    while True:
                        try:
                            chunk = q.get(timeout=0.5)
                        except queue.Empty:
                            # Detect a viewer closing while playback is paused.
                            # Without this heartbeat the handler would block on
                            # q.get forever and count a dead VLC connection.
                            readable, _, _ = select.select([self.connection], [], [], 0)
                            if (readable
                                    and self.connection.recv(1, socket.MSG_PEEK) == b""):
                                break
                            continue
                        self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
                    pass
                finally:
                    with broadcast.lock:
                        broadcast.clients.discard(q)
                        viewers = len(broadcast.clients)
                        if not viewers:
                            broadcast.viewer_event.clear()
                    broadcast.control.set_runtime(viewers=viewers,
                                                  playback_waiting=not viewers)
                    log(f"viewer left ({viewers} now)")

            def log_message(self, *a):  # silence per-request logging
                pass

        self.server = ThreadingHTTPServer((host, port), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def has_viewers(self) -> bool:
        return self.viewer_event.is_set()

    def wait_for_viewer(self, timeout: float) -> bool:
        return self.viewer_event.wait(timeout)

    def push(self, chunk: bytes) -> None:
        with self.lock:
            targets = list(self.clients)
        for q in targets:
            try:
                q.put_nowait(chunk)
            except queue.Full:
                pass  # slow viewer: drop rather than stall the whole stream


class Output:
    """Paced MPEG-TS sink; ``stdin`` is what the feeder writes clips into."""

    def __init__(self, args, broadcast: "Broadcast | None"):
        cmd = [args.ffmpeg, "-hide_banner", "-loglevel", "error", "-re",
               "-fflags", "+genpts", "-f", "mpegts", "-i", "pipe:0", "-c", "copy",
               "-mpegts_flags", "+resend_headers", "-pat_period", "0.1",
               "-f", "mpegts"]
        self.broadcast = broadcast
        if broadcast is not None:
            self.proc = subprocess.Popen(cmd + ["pipe:1"], stdin=subprocess.PIPE,
                                         stdout=subprocess.PIPE)
            threading.Thread(target=self._pump, daemon=True).start()
        else:
            self.proc = subprocess.Popen(cmd + [args.url], stdin=subprocess.PIPE)
        self.stdin = self.proc.stdin

    def _pump(self) -> None:
        while True:
            chunk = self.proc.stdout.read(1 << 15)
            if not chunk:
                break
            self.broadcast.push(chunk)

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def audio_stretch(rate: float, mode: str) -> str:
    """Filter chain that slows audio to ``rate`` without wrecking it.

    ``atempo`` is documented as valid from 0.5, so the 12 fps case runs it at
    exactly its limit -- its WSOLA overlap-add then leaves a periodic metallic
    ring on broadband material like rain and wind. rubberband is a real phase
    vocoder, has no such floor, and is the default for that reason.
    """
    if mode == "atempo":
        return f"atempo={rate:.8f}"
    if mode == "atempo2":                       # two gentler stages, product == rate
        half = rate ** 0.5
        return f"atempo={half:.8f},atempo={half:.8f}"
    if mode == "tape":                          # pitch falls with speed, as on tape
        return f"rubberband=tempo={rate:.8f}:pitch={rate:.8f}"
    return f"rubberband=tempo={rate:.8f}:transients=smooth"


def default_workflow() -> str:
    """Return the tested 448-square, 4-step Ref2V template in this package."""
    return os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "MiniMaxH3_R2V_4step_5s.json")


def drop_clip(path: str) -> None:
    """Delete a fed clip and any siblings the writer left next to it.

    VHS names the muxed file ``clip_00001-audio.mp4`` and the silent pass it
    was built from ``clip_00001.mp4``. VHS_KeepIntermediate=False should have
    removed the latter already; sweeping it here as well means a stale flag or
    an older VHS cannot quietly grow a file pile over a long run.
    """
    for p in ({path, path.replace("-audio.mp4", ".mp4")}
              if path.endswith("-audio.mp4") else {path}):
        try:
            os.remove(p)
        except OSError:
            pass


_AUDIO_DURATION_CACHE: dict[str, tuple[float, float]] = {}
MUSIC_EXTENSIONS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus"}


def _ffprobe_for(ffmpeg: str) -> str:
    """Prefer ffprobe beside ffmpeg, which also works with Winget builds."""
    folder = os.path.dirname(os.path.abspath(ffmpeg))
    suffix = ".exe" if os.name == "nt" else ""
    sibling = os.path.join(folder, "ffprobe" + suffix)
    return sibling if os.path.isfile(sibling) else (shutil.which("ffprobe") or "ffprobe")


def audio_duration(path: str, ffmpeg: str) -> float:
    """Return a cached duration so the loop seek never grows with stream age."""
    modified = os.path.getmtime(path)
    cached = _AUDIO_DURATION_CACHE.get(path)
    if cached and cached[0] == modified:
        return cached[1]
    cmd = [_ffprobe_for(ffmpeg), "-v", "error", "-show_entries",
           "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path]
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, timeout=15)
    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise RuntimeError("Musikdauer konnte nicht gelesen werden: "
                           + (result.stderr.strip() or path)) from exc
    if result.returncode or duration <= 0:
        raise RuntimeError("Ungültige Musikdatei: " + (result.stderr.strip() or path))
    _AUDIO_DURATION_CACHE[path] = (modified, duration)
    return duration


def music_files(source: str) -> list[str]:
    """Resolve either one audio file or a non-recursive music directory."""
    source = os.path.abspath(os.path.expanduser(source)) if source else ""
    if os.path.isfile(source):
        return [source]
    if not os.path.isdir(source):
        return []
    return [os.path.join(source, name)
            for name in sorted(os.listdir(source), key=str.casefold)
            if (os.path.isfile(os.path.join(source, name))
                and os.path.splitext(name)[1].casefold() in MUSIC_EXTENSIONS)]


class MusicPlaylist:
    """Stateful file/folder playlist that can cross boundaries inside a clip."""

    def __init__(self, ffmpeg: str):
        self.ffmpeg = ffmpeg
        self.signature = None
        self.order: list[str] = []
        self.index = 0
        self.position = 0.0

    def _reset(self, files: list[str], mode: str, signature) -> None:
        self.signature = signature
        self.order = list(files)
        if mode == "random":
            random.shuffle(self.order)
        self.index = 0
        self.position = 0.0

    def _advance(self, mode: str) -> None:
        previous = self.order[self.index] if self.order else None
        self.index += 1
        self.position = 0.0
        if self.index < len(self.order):
            return
        self.index = 0
        if mode == "random" and len(self.order) > 1:
            random.shuffle(self.order)
            if self.order[0] == previous:
                self.order[0], self.order[1] = self.order[1], self.order[0]

    def plan(self, source: str, mode: str,
             requested_s: float) -> list[tuple[str, float, float]]:
        files = music_files(source)
        if not files:
            return []
        mode = mode if mode in ("ordered", "random") else "ordered"
        signature = (os.path.abspath(os.path.expanduser(source)), mode, tuple(files))
        if signature != self.signature:
            self._reset(files, mode, signature)

        remaining = max(0.0, float(requested_s))
        segments: list[tuple[str, float, float]] = []
        # A safety limit prevents a folder of corrupt or millisecond-long files
        # from creating an unbounded ffmpeg command.
        for _ in range(256):
            if remaining <= 0.001:
                break
            current = self.order[self.index]
            duration = audio_duration(current, self.ffmpeg)
            available = max(0.0, duration - self.position)
            if available <= 0.001:
                self._advance(mode)
                continue
            take = min(remaining, available)
            segments.append((current, self.position, take))
            self.position += take
            remaining -= take
            if self.position >= duration - 0.001:
                self._advance(mode)
        if remaining > 0.01:
            raise RuntimeError("Music playlist contains no usable audio duration")
        return segments


def feed_clip(path: str, offset: float, args, sink, settings: dict,
              clip_play: float, playlist: MusicPlaylist | None = None) -> str:
    """Retime one clip, optionally mix a continuous bed, and emit MPEG-TS."""
    rate = args.fps / 24.0
    music_path = str(settings.get("music_path", ""))
    segments: list[tuple[str, float, float]] = []
    music_error = ""
    if settings.get("external_music_enabled") and music_path:
        try:
            playlist = playlist or MusicPlaylist(args.ffmpeg)
            segments = playlist.plan(
                music_path, settings.get("music_play_mode", "ordered"), clip_play)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            music_error = str(exc)
    use_music = bool(segments)
    h3_gain = float(settings.get("h3_audio_volume", 1.0))
    music_gain = float(settings.get("music_volume", 0.20))
    inputs = [args.ffmpeg, "-hide_banner", "-loglevel", "error", "-i", path]
    for music_file, start, duration in segments:
        inputs += ["-ss", f"{start:.6f}", "-t", f"{duration:.6f}",
                   "-i", music_file]

    # Adaptive generation may alternate render dimensions. Normalize to one
    # canvas so VLC never has to survive a mid-stream H.264 resize.
    filters = (f"[0:v]scale={args.stream_width}:{args.stream_height}:flags=lanczos,"
               f"setpts=PTS/{rate:.6f}[v];"
               f"[0:a]{audio_stretch(rate, args.stretch)}"
               # loudnorm emits 96 kHz internally; pin it back to AAC's rate.
               + (f",loudnorm=I={args.lufs}:TP=-1.5:LRA=11" if args.lufs else "")
               + f",aresample={args.arate},volume={h3_gain:.4f}")
    if use_music:
        filters += ("[h3pre];" if settings.get("music_ducking") else "[h3];")
        music_labels = []
        for index, (_music_file, _start, duration) in enumerate(segments, 1):
            label = f"musicpart{index}"
            filters += (f"[{index}:a]aresample={args.arate},"
                        f"atrim=duration={duration:.6f},apad=whole_dur={duration:.6f},"
                        f"atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[{label}];")
            music_labels.append(f"[{label}]")
        if len(music_labels) == 1:
            filters += f"{music_labels[0]}anull[musicraw];"
        else:
            filters += ("".join(music_labels)
                        + f"concat=n={len(music_labels)}:v=0:a=1[musicraw];")
        filters += f"[musicraw]volume={music_gain:.4f}[musicgain];"
    if use_music and settings.get("music_ducking"):
        filters += ("[h3pre]asplit=2[h3][side];"
                    "[musicgain][side]sidechaincompress=threshold=0.025:ratio=8:"
                    "attack=20:release=500[bed];"
                    "[h3][bed]amix=inputs=2:duration=first:dropout_transition=0:"
                    "normalize=0,alimiter=limit=0.95[a]")
    elif use_music:
        filters += ("[h3][musicgain]amix=inputs=2:duration=first:dropout_transition=0:"
                    "normalize=0,alimiter=limit=0.95[a]")
    else:
        filters += "[a]"

    cmd = inputs + ["-filter_complex", filters,
           "-map", "[v]", "-map", "[a]", "-r", str(args.fps),
           "-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
           "-g", "24", "-keyint_min", "24", "-sc_threshold", "0",
           "-pix_fmt", "yuv420p", "-b:v", args.vbitrate,
           "-c:a", "aac", "-b:a", "128k", "-ar", str(args.arate), "-ac", "2",
           "-muxdelay", "0", "-output_ts_offset", f"{offset:.3f}",
           # identical PIDs on every clip, so a boundary is not a PMT change,
           # and frequent tables so a viewer joining mid-clip finds the audio ES
           "-streamid", "0:256", "-streamid", "1:257",
           "-mpegts_flags", "+resend_headers", "-pat_period", "0.1", "-sdt_period", "0.5",
           "-f", "mpegts", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        shutil.copyfileobj(proc.stdout, sink, length=1 << 16)
    finally:
        proc.stdout.close()
        stderr = proc.stderr.read().decode("utf-8", "replace")
        code = proc.wait()
        proc.stderr.close()
    if code:
        raise RuntimeError("FFmpeg clip/audio mix failed: " + stderr.strip()[-600:])
    if use_music:
        duck = ", ducking" if settings.get("music_ducking") else ""
        names = []
        for music_file, _start, _duration in segments:
            name = os.path.basename(music_file)
            if not names or names[-1] != name:
                names.append(name)
        source_mode = (settings.get("music_play_mode", "ordered")
                       if os.path.isdir(music_path) else "single")
        return (f"H3 {h3_gain:g} + {' -> '.join(names)} {music_gain:g} "
                f"({source_mode}{duck})")
    if settings.get("external_music_enabled"):
        return "H3 audio only – " + (music_error or "music source empty/missing")
    return f"H3 audio {h3_gain:g}"



def script_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def default_comfy_data_root() -> str:
    """Find ComfyUI's input/output root for Desktop or Portable installs.

    The launcher passes explicit paths, so this primarily covers users who run
    the Python file directly. Current Comfy Desktop stores shared input/output
    data below LocalAppData; older Desktop versions used a home-directory
    ComfyUI-Shared folder. A repository merged into the Windows portable build
    continues to use the adjacent ComfyUI directory.
    """
    override = os.environ.get("COMFYUI_DATA_ROOT", "").strip()
    if override:
        return os.path.abspath(os.path.expandvars(os.path.expanduser(override)))

    adjacent_portable = os.path.join(os.path.dirname(script_dir()), "ComfyUI")
    candidates = [adjacent_portable]

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        candidates.append(os.path.join(
            local_app_data, "Comfy-Desktop", "ComfyUI-Shared"))

    user_profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
    if user_profile:
        candidates.extend([
            os.path.join(user_profile, "ComfyUI-Shared"),
            os.path.join(user_profile, "Documents", "ComfyUI"),
        ])

    for candidate in candidates:
        if os.path.isdir(candidate):
            return os.path.abspath(candidate)

    # On a fresh Windows Desktop installation the folder may not exist until
    # ComfyUI has launched once. Return the current official default so a later
    # error names the useful path rather than an unrelated working directory.
    if local_app_data:
        return os.path.abspath(os.path.join(
            local_app_data, "Comfy-Desktop", "ComfyUI-Shared"))
    return os.path.abspath(adjacent_portable)


def default_comfy_input() -> str:
    return os.path.join(default_comfy_data_root(), "input")


def default_comfy_output() -> str:
    return os.path.join(default_comfy_data_root(), "output")


def resolve_ffmpeg(value: str) -> str:
    """Resolve an explicit path or the ffmpeg installed in PATH."""
    candidate = os.path.expanduser(value.strip()) if value else "ffmpeg"
    if os.path.isfile(candidate):
        return os.path.abspath(candidate)
    found = shutil.which(candidate)
    if found:
        return found
    raise SystemExit(
        f"FFmpeg nicht gefunden: {candidate!r}. Installiere es mit "
        "'winget install -e --id Gyan.FFmpeg', öffne PowerShell neu, "
        "oder übergib --ffmpeg <vollstaendiger-pfad>\\ffmpeg.exe"
    )

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--workflow", default=default_workflow(),
                   help="UI workflow to convert and submit. Defaults to the copy in "
                        "ComfyUI's workflow folder if it is there, else the one shipped "
                        "beside this script.")
    p.add_argument("--scenes", default=os.path.join(script_dir(), "prompts_scenes.txt"),
                   help="scene file; blocks separated by a line containing ---, each with {NAME}")
    p.add_argument("--characters", default=os.path.join(script_dir(), "h3_characters.json"),
                   help="written by build_h3_characters.py")
    p.add_argument("--refs-dir", default=os.path.join(script_dir(), "character_refs"),
                   help="folder containing character reference images named by alias")
    p.add_argument("--control-url", default="http://127.0.0.1:9001",
                   help="local control UI URL")
    p.add_argument("--control-config", default=os.path.join(script_dir(), "h3_r2v_continuity.json"),
                   help="persistent live-UI settings")
    p.add_argument("--start-paused", action="store_true",
                   help="start with generation disabled (saved UI state still takes precedence)")
    p.add_argument("--ffmpeg", default="ffmpeg",
                   help="ffmpeg executable or absolute path")
    p.add_argument("--ref-image-size", default="match", choices=["match", "max"],
                   help="R2V reference image sizing; max improves identity but is slower")
    p.add_argument("--comfy-input", default=default_comfy_input(),
                   help="ComfyUI input directory; references are copied here before submission")
    p.add_argument("--comfy-output", default=default_comfy_output(),
                   help="ComfyUI output directory used to collect generated clips")
    p.add_argument("--curated-share", type=float, default=0.30,
                   help="fraction of draws taken from the high-recognition pool")
    p.add_argument("--url", default="http://127.0.0.1:9000",
                   help="http://HOST:PORT (VLC opens the same URL) or udp://HOST:PORT")
    p.add_argument("--fps", type=float, default=14.0,
                   help="playback rate; frames are authored at 24, so this slows motion by fps/24")
    p.add_argument("--width", type=int, default=448)
    p.add_argument("--height", type=int, default=448)
    p.add_argument("--length", type=int, default=124,
                   help="initial frame count; H3 aligns lengths to 17k+5 (124 is about 5 s native)")
    p.add_argument("--seed", type=int, default=90000,
                   help="first noise seed; each clip takes the next one up. Logged per "
                        "clip, so a clip worth keeping can be reproduced from its log "
                        "line together with the scene index printed beside it.")
    p.add_argument("--prefill", type=int, default=3, help="clips to bank before opening the stream")
    p.add_argument("--queue-max", type=int, default=8)
    p.add_argument("--vbitrate", default="4M",
                   help="stream H.264 bitrate; 4M avoids throwing away detail on the 800px HQ canvas")
    p.add_argument("--stretch", default="rubberband",
                   choices=["rubberband", "atempo", "atempo2", "tape"],
                   help="how the audio is slowed to match --fps; atempo bottoms out at 0.5 "
                        "(= 12 fps) and rings on broadband sound, rubberband does not")
    p.add_argument("--arate", type=int, default=48000,
                   help="output audio sample rate; must be a normal AAC rate")
    p.add_argument("--lufs", type=float, default=0.0,
                   help="EBU R128 loudness target, e.g. -16. Off by default: H3's own level "
                        "is fine on a correctly configured output, and loudnorm adds a "
                        "resampling stage and dynamic gain the stream does not need.")
    p.add_argument("--music", default=os.path.join(script_dir(), "music.mp3"),
                   help="initial external music path shown in the control UI")
    p.add_argument("--dit", default="minimax_h3_ref2va_pruned_int8_convrot.safetensors")
    p.add_argument("--clip", default="qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors")
    # x264 over nvenc: both land at ~1.6 s, because what is left after moving
    # the encode out of Python is the per-frame tobytes() and the pipe, not the
    # encoder. Given a tie, libx264 avoids standing up a second CUDA context on
    # a card with ~3 GB free, and is kinder to the re-encode that follows.
    p.add_argument("--writer", default="h3fast",
                   choices=["h3fast", "vhs-x264", "vhs-nvenc", "savevideo"],
                   help="how ComfyUI writes the clip. savevideo is the stock node and "
                        "the slowest by far; the vhs- writers pipe raw frames to ffmpeg")
    p.add_argument("--pipeline", type=int, default=2,
                   help="prompts kept in ComfyUI's queue, so the GPU does not idle "
                        "between jobs; 1 restores the old submit-and-wait behaviour")
    p.add_argument("--video-vae", default="minimax_h3_video_vae_fp16.safetensors",
                   help="video VAE to load; the audio VAE is left alone")
    p.add_argument("--vae-tile", type=int, default=0,
                   help="H3 video VAE spatial tile edge in pixels. 256 is the only value "
                        "that decodes correctly; larger is faster and visibly worse. "
                        "0 leaves whatever is currently set on the loaded VAE.")
    p.add_argument("--window", type=int, default=8,
                   help="clips averaged for the sustainable-fps readout; a trailing "
                        "window rather than the all-time mean, which a slow cold start "
                        "would poison for hundreds of clips")
    p.add_argument("--write-timeout", type=float, default=60.0,
                   help="how long to wait for an asynchronously written clip to appear "
                        "before giving up on it and moving to the next")
    p.add_argument("--keep-clips", action="store_true", help="do not delete clips after streaming")
    p.add_argument("--max-clips", type=int, default=0, help="stop after N clips (0 = forever); for smoke tests")
    args = p.parse_args()
    args.ffmpeg = resolve_ffmpeg(args.ffmpeg)

    if args.length != 124:
        log("warning: this continuity preset was tuned for 124-frame clips")

    # sweep anything a previous run left behind, so the scratch dir never grows
    scratch = os.path.join(args.comfy_output, os.path.dirname(SCRATCH_PREFIX))
    if os.path.isdir(scratch):
        # .part too: the async writer's encoder thread is a daemon, so a clip
        # in flight when ComfyUI or this script is killed leaves one behind
        # with no chance to run its own cleanup.
        stale = [f for f in os.listdir(scratch) if f.endswith((".mp4", ".png", ".part"))]
        for f in stale:
            try:
                os.remove(os.path.join(scratch, f))
            except OSError:
                pass
        if stale:
            log(f"swept {len(stale)} stale clip(s) from {scratch}")

    continuity_dir = os.path.join(args.comfy_input, CONTINUITY_SUBDIR)
    if os.path.isdir(continuity_dir):
        stale_frames = [f for f in os.listdir(continuity_dir)
                        if f.startswith("last_frame_") and f.endswith(".png")]
        for filename in stale_frames:
            try:
                os.remove(os.path.join(continuity_dir, filename))
            except OSError:
                pass

    q: queue.Queue = queue.Queue(maxsize=args.queue_max)
    stop = threading.Event()
    control = ControlState(args.control_config, args.scenes, args.refs_dir,
                           start_enabled=not args.start_paused,
                           fast_width=args.width, fast_height=args.height,
                           music_path=args.music, clip_length=args.length,
                           playback_fps=args.fps)
    initial_quality = control.snapshot()
    initial_length = initial_quality["clip_length"]
    initial_play = initial_length / args.fps
    log(f"{args.width}x{args.height} x {initial_length} frames  "
        f"writer={args.writer}  pipeline={args.pipeline}")
    log(f"playback {initial_play:.2f}s per clip at {args.fps:g} fps "
        f"(motion at {args.fps / 24 * 100:.0f}% speed); generation duration is "
        "live-adjustable")
    fast_area = initial_quality["fast_width"] * initial_quality["fast_height"]
    quality_area = initial_quality["quality_width"] * initial_quality["quality_height"]
    if initial_quality["adaptive_quality"] and quality_area >= fast_area:
        args.stream_width = initial_quality["quality_width"]
        args.stream_height = initial_quality["quality_height"]
    else:
        args.stream_width = initial_quality["fast_width"]
        args.stream_height = initial_quality["fast_height"]
    log("adaptive quality " + ("enabled" if initial_quality["adaptive_quality"] else "disabled")
        + f": fast {initial_quality['fast_width']}x{initial_quality['fast_height']}, "
          f"HQ {initial_quality['quality_width']}x{initial_quality['quality_height']}, "
          f"buffer thresholds {initial_quality['quality_low_water']}/"
          f"{initial_quality['quality_high_water']}")
    log(f"stream canvas: {args.stream_width}x{args.stream_height} "
        "(fixed until restart; adaptive clips are normalized to this size)")
    pool = PromptPool(control.snapshot()["scenes_path"], args.characters, args.curated_share)
    producer = Producer(args, q, stop, pool, control)

    control_parsed = urlparse(args.control_url)
    control_host = control_parsed.hostname or "127.0.0.1"
    control_port = control_parsed.port or 9001
    ControlServer(control_host, control_port, control, producer.metadata)
    log(f"CONTROL UI -- open in browser:  {args.control_url}")
    producer.start()

    broadcast = None
    if args.url.startswith("http"):
        hostport = args.url.split("//", 1)[1].split("/")[0]
        host, _, port = hostport.partition(":")
        broadcast = Broadcast(host or "127.0.0.1", int(port or 8080), control)
        log(f"STREAM OPEN -- open this in VLC now:  {args.url}")
        log("  you may connect during prefill; playback starts when clips arrive")
    else:
        control.set_runtime(viewers=None, playback_waiting=False)
        log(f"stream target:  {args.url.replace('udp://', 'udp://@')}  (open after prefill)")

    log(f"prefilling {args.prefill} clips")
    seen = -1
    while q.qsize() < args.prefill and not stop.is_set():
        if q.qsize() != seen:
            seen = q.qsize()
            log(f"  prefill {seen}/{args.prefill}")
        time.sleep(0.5)

    out = Output(args, broadcast)
    # ffmpeg does not bind the HTTP listener until it has probed its input, so
    # the port is dead for the whole prefill plus the first clip's first bytes.
    # Announce the URL only once a TCP connect actually succeeds.
    if broadcast is None:
        log(f"STREAM OPEN -- in VLC use:  {args.url.replace('udp://', 'udp://@')}")

    offset = 0.0
    played = 0
    # A trailing window, not the all-time mean: the first clip after a cold
    # ComfyUI is always several seconds slow, and an all-time mean carries that
    # for hundreds of clips, which is exactly long enough to mislead someone
    # deciding whether the current --fps is sustainable.
    recent: collections.deque[tuple[float, int]] = collections.deque(maxlen=args.window)
    t_start = time.time()
    waiting_for_viewer = False
    last_audio_mode = None
    music_playlist = MusicPlaylist(args.ffmpeg)
    try:
        while True:
            if broadcast is not None and not broadcast.has_viewers():
                control.set_runtime(buffered=q.qsize(), playback_waiting=True)
                if not waiting_for_viewer:
                    log("no viewer connected: playback paused; building video buffer")
                    waiting_for_viewer = True
                broadcast.wait_for_viewer(0.5)
                continue
            if waiting_for_viewer:
                log(f"viewer available: playback resumes with {q.qsize()} buffered clip(s)")
                waiting_for_viewer = False
            control.set_runtime(playback_waiting=False)
            try:
                path, gen_s, scene_idx, seed, clip_length = q.get(timeout=0.25)
            except queue.Empty:
                continue
            depth = q.qsize()
            control.set_runtime(buffered=depth)
            playback_settings = control.snapshot()
            clip_play = clip_length / args.fps
            audio_mode = feed_clip(path, offset, args, out.stdin, playback_settings,
                                   clip_play, music_playlist)
            out.stdin.flush()
            control.set_runtime(audio_mode=audio_mode)
            if audio_mode != last_audio_mode:
                log("audio: " + audio_mode)
                last_audio_mode = audio_mode
            offset += clip_play
            played += 1
            # The first clip's "interval" is measured from before ComfyUI had
            # loaded anything, so on a cold server it is a model load plus a
            # generation -- 40 s or more against a steady state near 20 s. Left
            # in an 8-wide window it drags the mean for the window's whole
            # length, which is long enough to report BEHIND for the entire
            # opening of every run. It is a load time, not a throughput sample.
            if played > 1:
                recent.append((gen_s, clip_length))
            if not args.keep_clips:
                drop_clip(path)

            head = (f"streamed {played:04d}  scene {scene_idx:03d} seed {seed:<10d} "
                    f"buf={depth}  gen {gen_s:5.1f}s ")
            if not recent:
                log(head + "(cold start, excluded from the average)")
            else:
                # The whole question this script exists to answer: is generation
                # keeping up, and if not, what --fps would it keep up with?
                mean = sum(seconds for seconds, _frames in recent) / len(recent)
                total_generation = sum(seconds for seconds, _frames in recent)
                total_frames = sum(frames for _seconds, frames in recent)
                can_do = total_frames / total_generation
                predicted_generation = clip_length / can_do
                margin = clip_play - predicted_generation
                verdict = "OK" if margin >= 0 else "BEHIND"
                if margin < 0 and depth:
                    with q.mutex:
                        buffer_seconds = sum(item[4] / args.fps for item in q.queue)
                    verdict += f" ~{int(buffer_seconds / -margin)} clips of runway"
                elif margin < 0:
                    verdict += " buffer empty"
                log(head + f"(avg{len(recent)} {mean:5.1f})  play {clip_play:5.2f}s  "
                           f"margin {margin:+5.2f}s/clip  sustains {can_do:4.1f}fps  "
                           f"{verdict}")
            if args.max_clips and played >= args.max_clips:
                log("max-clips reached")
                break
    except (KeyboardInterrupt, BrokenPipeError):
        log("stopping")
    finally:
        stop.set()
        if not args.keep_clips:
            while not q.empty():
                leftover, _, _, _, _ = q.get()
                drop_clip(leftover)
        out.close()


if __name__ == "__main__":
    sys.exit(main())
