"""Seeded, per-stage random number streams.

Each pipeline stage gets its own independent :class:`numpy.random.Generator`, derived from the
global seed and a fixed stage name. Because the stream is keyed by the *name* (not by the order
in which stages ask for one), changing or adding a stage never changes another stage's numbers.
"""

import zlib

import numpy as np


def stage_seed_sequence(seed: int, stage: str) -> np.random.SeedSequence:
    """Return the child seed sequence for one stage.

    This is equivalent to ``SeedSequence(seed).spawn(...)`` (which works by setting
    ``spawn_key``), except that the key is a stable CRC32 hash of the stage name.

    Args:
        seed: Global non-negative integer seed.
        stage: Fixed, non-empty stage name, e.g. ``"master"``.

    Returns:
        The stage's :class:`numpy.random.SeedSequence`.

    Raises:
        ValueError: If ``seed`` is negative or ``stage`` is empty.
    """
    if isinstance(seed, bool) or not isinstance(seed, int) or seed < 0:
        raise ValueError(f"seed must be a non-negative int, got {seed!r}")
    if not stage or not stage.strip():
        raise ValueError("stage name must be a non-empty string")
    key = zlib.crc32(stage.encode("utf-8"))
    return np.random.SeedSequence(entropy=seed, spawn_key=(key,))


def stage_rng(seed: int, stage: str) -> np.random.Generator:
    """Return a fresh, deterministic random generator for one stage.

    Args:
        seed: Global non-negative integer seed.
        stage: Fixed, non-empty stage name.

    Returns:
        A :class:`numpy.random.Generator` (PCG64) seeded for that stage.
    """
    return np.random.Generator(np.random.PCG64(stage_seed_sequence(seed, stage)))
