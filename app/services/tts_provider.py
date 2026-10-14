"""TTS provider adapter seam (phase 4).

MVP keeps the built-in Edge TTS. A future CosyVoice2 (Alibaba) custom cloned
voice plugs in here without touching the pipeline: callers use
``synthesize()`` and receive the same 3-tuple the pipeline already expects
(audio path, duration, sub-maker).

Design notes
------------
* ``PROVIDERS`` maps a provider id to a callable; ``active_provider()`` reads
  ``config.app["tts_provider"]`` so switching is a config change, not a code
  change.
* Unimplemented providers (cosyvoice2) raise ``NotImplementedError`` with a
  clear message *only when selected*, so shipping the seam cannot break the
  existing Edge TTS path.
* Every provider must return an object exposing ``cues``/``get_srt()`` like
  ``edge_tts.SubMaker``; ``_NullSubMaker`` is provided for providers that emit
  only audio, letting subtitle generation fall back to whisper.
"""
from __future__ import annotations

from typing import Any, Callable, Union

from loguru import logger

from app.config import config

# Provider ids that are wired up today.
SUPPORTED_PROVIDERS = ("edge", "cosyvoice2")

DEFAULT_PROVIDER = "edge"


class _NullSubMaker:
    """Stand-in sub-maker for audio-only providers.

    ``task.generate_subtitle`` checks ``sub_maker is None`` to decide whether
    whisper alignment must run; returning a real (empty) object instead keeps
    that decision explicit and documented rather than implicit.
    """

    def __init__(self) -> None:
        self.cues: list[Any] = []

    def get_srt(self) -> str:
        return ""


def active_provider() -> str:
    provider = str(config.app.get("tts_provider", DEFAULT_PROVIDER) or "").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        logger.warning(
            f"unknown tts_provider {provider!r}, falling back to {DEFAULT_PROVIDER}"
        )
        return DEFAULT_PROVIDER
    return provider


def _synthesize_edge(
    text: str,
    voice_name: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float | None = None,
    **extra_kwargs,
):
    """Edge TTS through the project's voice service.

    ``voice_volume`` is only forwarded when the caller explicitly supplied one;
    the video stage owns volume gain, so passing the default would apply it
    twice (regression: test_non_default_volume_regenerates_audio_without_double_gain).
    ``**extra_kwargs`` carries provider-specific options added upstream (e.g.
    VoxCPM voice-cloning ``voxcpm_reference_audio``/``voxcpm_prompt_audio``/
    ``voxcpm_prompt_text``) straight through to ``voice.tts``.
    """
    from app.services import voice

    kwargs = {
        "text": text,
        "voice_name": voice_name,
        "voice_rate": voice_rate,
        "voice_file": voice_file,
    }
    if voice_volume is not None:
        kwargs["voice_volume"] = voice_volume
    kwargs.update(extra_kwargs)
    return voice.tts(**kwargs)


def _synthesize_cosyvoice2(
    text: str,
    voice_name: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float | None = None,
    **extra_kwargs,
):
    """Reserved: Alibaba Cloud CosyVoice2 custom cloned voice.

    Intentionally not implemented. To enable:
      1. add ``cosyvoice2_api_key`` / ``cosyvoice2_base_url`` to config.toml
         (``DASHSCOPE_API_KEY`` is the expected env var),
      2. set ``voice_name`` to a registered cloned-voice id,
      3. call the CosyVoice2 streaming API and wrap the word-level timestamps
         into a SubMaker-compatible object,
      4. set ``tts_provider = "cosyvoice2"``.
    The pipeline needs no other change — duration and sub-maker flow through
    this same signature.
    """
    raise NotImplementedError(
        "CosyVoice2 TTS is not implemented yet. Set tts_provider = \"edge\" "
        "for now; see app/services/tts_provider.py for the integration steps."
    )


PROVIDERS: dict[str, Callable[..., Union[Any, None]]] = {
    "edge": _synthesize_edge,
    "cosyvoice2": _synthesize_cosyvoice2,
}


def synthesize(
    text: str,
    voice_name: str,
    voice_rate: float,
    voice_file: str,
    voice_volume: float | None = None,
    **extra_kwargs,
):
    """Synthesize speech through the configured provider.

    Returns the provider's sub-maker (or None on failure), matching
    ``voice.tts``'s contract so callers stay provider-agnostic.
    ``voice_volume`` is optional: the video stage owns gain, so callers that do
    not explicitly need TTS-side volume must leave it unset.
    ``**extra_kwargs`` is forwarded to the underlying provider so upstream-only
    options (VoxCPM voice cloning, etc.) pass through without breaking the seam.
    """
    provider = active_provider()
    fn = PROVIDERS.get(provider, _synthesize_edge)
    logger.info(f"tts provider: {provider}")
    return fn(
        text=text,
        voice_name=voice_name,
        voice_rate=voice_rate,
        voice_file=voice_file,
        voice_volume=voice_volume,
        **extra_kwargs,
    )
