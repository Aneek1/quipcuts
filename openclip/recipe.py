"""Clip recipes: everything needed to re-render a clip, as editable JSON.

The pipeline used to go straight from an LLM pick to a finished MP4. A recipe sits in between:
the source video plus start/end, crop path, caption words, layout and caption style. The web
editor changes the recipe; the MP4 is rendered from it on export, and `render_hash` says whether
the current file still matches the recipe.

All times in a recipe are absolute seconds in the source video, like the analyzer's output.
"""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from .renderer import render_final_clip

RECIPE_VERSION = 1


class Word(BaseModel):
    word: str
    start: float
    end: float
    loud: float | None = None    # loudness z-score within the clip (openclip/emphasis.py); drives punch-in zooms


class ZoomStyle(BaseModel):
    """Punch-in zooms on the loudest words."""
    enabled: bool = False
    strength: float = Field(0.08, ge=0.03, le=0.2)   # 0.08 = zoom in 8%


class Keyframe(BaseModel):
    time: float          # seconds from the clip start
    x: float
    y: float
    w: float
    h: float
    cut: bool = False    # a scene cut starts here: jump, don't pan from the previous keyframe


class Crop(BaseModel):
    single: list[Keyframe] | None = None
    face_1: list[Keyframe] | None = None
    face_2: list[Keyframe] | None = None


HEX = r"^#[0-9A-Fa-f]{6}$"


class CaptionStyle(BaseModel):
    font: str | None = None      # None = renderer default (Montserrat Black when installed)
    size: int = Field(96, ge=24, le=200)
    enabled: bool = True
    position: float | None = Field(None, ge=0.1, le=0.9)   # centre as a fraction of height; None = layout default
    highlight: str = Field("#FFFF00", pattern=HEX)          # colour of the word being spoken
    uppercase: bool = True


class Overlay(BaseModel):
    """Text on top of the clip for a stretch of time: a hook at the start, a CTA at the end, a label."""
    kind: Literal["hook", "cta", "text"] = "text"
    text: str = Field(min_length=1, max_length=80)
    start: float = Field(ge=0)   # seconds from the clip start
    end: float
    position: Literal["top", "middle", "bottom"] = "top"
    style: Literal["light", "dark", "accent", "plain"] = "light"


class Source(BaseModel):
    key: str                     # storage key of the source video
    width: int
    height: int
    duration: float
    fps: float | None = None


class Recipe(BaseModel):
    version: int = RECIPE_VERSION
    source: Source
    start: float = Field(ge=0)
    end: float
    title: str
    score: int | None = None
    reason: str = ""
    layout: Literal["single", "split"] = "single"
    crop: Crop
    words: list[Word]
    captions: CaptionStyle = CaptionStyle()
    overlays: list[Overlay] = Field(default_factory=list, max_length=6)
    zoom: ZoomStyle = ZoomStyle()

    @model_validator(mode="after")
    def _check(self) -> "Recipe":
        if self.end <= self.start:
            raise ValueError("end must be after start")
        if self.end > self.source.duration + 0.5:
            raise ValueError("end is past the end of the source video")
        if self.end - self.start > 180:
            raise ValueError("clips are capped at 3 minutes")
        for o in self.overlays:
            if o.end <= o.start or o.start >= self.end - self.start:
                raise ValueError(f"the text '{o.text}' has to start and end inside the clip")
        if self.layout == "single" and not self.crop.single:
            raise ValueError("single layout needs crop.single keyframes")
        if self.layout == "split" and not (self.crop.face_1 and self.crop.face_2):
            raise ValueError("split layout needs crop.face_1 and crop.face_2 keyframes")
        return self


def from_pipeline(clip: dict, keyframes, layout: str, source: Source, media_path: str | None = None) -> Recipe:
    """Build a recipe from an analyzer clip and the cropper's keyframes.

    With `media_path` (anything ffmpeg can read the audio from), words get loudness scores and
    punch-in zooms are switched on.
    """
    crop = Crop(single=keyframes) if layout == "single" else Crop(face_1=keyframes["face_1"],
                                                                     face_2=keyframes["face_2"])
    words = [Word(word=w["word"], start=w["start"], end=w["end"]) for w in clip.get("words", [])]
    recipe = Recipe(source=source, start=clip["start_time"], end=clip["end_time"],
                    title=clip["headline_title"], score=clip.get("virality_score"),
                    reason=clip.get("virality_score_rationale", ""), layout=layout, crop=crop, words=words)
    if media_path:
        add_loudness(recipe, media_path)
        recipe.zoom.enabled = True
    return recipe


def add_loudness(recipe: Recipe, media_path: str) -> bool:
    """Fill in Word.loud where it's missing (e.g. after a trim pulled in new words). True if anything changed."""
    from .emphasis import word_loudness

    if all(w.loud is not None for w in recipe.words) or not recipe.words:
        return False
    try:
        scores = word_loudness(media_path, recipe.start, recipe.end, [w.model_dump() for w in recipe.words])
    except Exception:  # a clip without readable audio just gets no zooms
        return False
    for w, z in zip(recipe.words, scores):
        w.loud = z
    return True


# Fields added after the first recipes were rendered. Left out of the hash while they hold their
# defaults, so clips rendered before they existed don't all show up as out of date.
_LATER_CAPTION_FIELDS = {"enabled": True, "position": None, "highlight": "#FFFF00", "uppercase": True}


def render_hash(recipe: Recipe) -> str:
    """Changes whenever anything that affects the rendered pixels or audio changes."""
    data = recipe.model_dump(exclude={"score", "reason"})
    for k, default in _LATER_CAPTION_FIELDS.items():
        if data["captions"].get(k) == default:
            data["captions"].pop(k)
    if not data["overlays"]:
        data.pop("overlays")
    if not data["zoom"]["enabled"]:
        data.pop("zoom")
    for w in data["words"]:
        if w.get("loud") is None or "zoom" not in data:   # loudness only matters once zooms are on
            w.pop("loud", None)
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()[:16]


def render_recipe(recipe: Recipe, source_path: str, out_path: str, preset: str = "medium") -> str:
    clip = {"start_time": recipe.start, "end_time": recipe.end,
            "words": [w.model_dump() for w in recipe.words]}
    if recipe.layout == "split":
        keyframes = {"face_1": [k.model_dump() for k in recipe.crop.face_1],
                     "face_2": [k.model_dump() for k in recipe.crop.face_2]}
    else:
        keyframes = [k.model_dump() for k in recipe.crop.single]
    cap = recipe.captions
    zooms = []
    if recipe.zoom.enabled:
        from .emphasis import pick_zooms
        add_loudness(recipe, source_path)
        zooms = pick_zooms([w.model_dump() for w in recipe.words], recipe.start, recipe.end)
    return render_final_clip(source_path, clip, keyframes, out_path, font_name=cap.font,
                             font_size=cap.size, layout=recipe.layout, preset=preset,
                             captions=cap.enabled, caption_position=cap.position,
                             highlight=cap.highlight, uppercase=cap.uppercase,
                             overlays=[o.model_dump() for o in recipe.overlays],
                             zooms=zooms, zoom_strength=recipe.zoom.strength)
