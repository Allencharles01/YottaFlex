import numpy as np
import pytest

from coldchain.rng import stage_rng, stage_seed_sequence

SEED = 20261022


def test_same_seed_and_stage_is_deterministic() -> None:
    a = stage_rng(SEED, "master").random(10)
    b = stage_rng(SEED, "master").random(10)
    np.testing.assert_array_equal(a, b)


def test_known_values_are_stable_across_runs() -> None:
    # Pin the stream so an accidental change to the derivation is caught.
    assert stage_rng(SEED, "master").integers(0, 1_000_000, size=3).tolist() == [
        851145,
        654078,
        735306,
    ]
    assert stage_seed_sequence(SEED, "master").spawn_key == (755606486,)  # zlib.crc32(b"master")


def test_different_stages_are_independent() -> None:
    a = stage_rng(SEED, "master").random(10)
    b = stage_rng(SEED, "readings").random(10)
    assert not np.array_equal(a, b)


def test_different_seeds_differ() -> None:
    a = stage_rng(SEED, "master").random(10)
    b = stage_rng(SEED + 1, "master").random(10)
    assert not np.array_equal(a, b)


def test_stream_independent_of_call_order() -> None:
    # Drawing from another stage first must not change this stage's numbers.
    expected = stage_rng(SEED, "readings").random(5)
    stage_rng(SEED, "master").random(1000)
    np.testing.assert_array_equal(stage_rng(SEED, "readings").random(5), expected)


def test_returns_numpy_generator() -> None:
    assert isinstance(stage_rng(SEED, "x"), np.random.Generator)


@pytest.mark.parametrize(("seed", "stage"), [(-1, "master"), (SEED, ""), (SEED, "  "), (1.5, "a")])
def test_invalid_arguments(seed: object, stage: str) -> None:
    with pytest.raises(ValueError):
        stage_rng(seed, stage)  # type: ignore[arg-type]
