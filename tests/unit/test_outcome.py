"""The vocabulary behind "why didn't it say anything just now".

Plan §4.12 makes the `(outcome, phase)` pair the machine-readable answer to the
number one support question for a live product, and the control panel renders
`str(Verdict)` straight into the event stream.

The real contract — the scheduler emits exactly one Verdict per Intent — needs a
scheduler that does not exist yet. What is testable today is the vocabulary and
the pairing. `SkipReason` is documented as append-only because the strings get
aggregated into stats; nothing else in the tree enforces that, so a rename would
otherwise land silently and only show up as a hole in a dashboard.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from bilisama.obs.outcome import Outcome, Phase, SkipReason, Verdict

# Pinned from plan §4.12. Renaming a member goes red here; appending one does not.
SKIP_REASONS = {
    "LOW_VALUE": "selection.low_value",
    "DUPLICATE": "selection.duplicate",
    "RATE_LIMITED": "selection.rate_limited",
    "QUEUE_FULL": "selection.queue_full",
    "SPEAK_DISABLED": "policy.speak_disabled",
    "HOST_SPEAKING": "gate.host_speaking",
    "TURN_PENDING": "gate.turn_pending",
    "AUDIO_QUEUED": "gate.audio_queued",
    "INJECTION_GATE": "gate.injection_window",
    "COOLDOWN": "gate.cooldown",
    "PREEMPTED": "scheduler.preempted",
    "RESULT_EXPIRED": "background.result_expired",
    "PANIC_MUTE": "policy.panic_mute",
    "OUTPUT_BLOCKED": "safety.output_blocked",
}


def _verdict(outcome: Outcome, phase: Phase, reason: SkipReason | None = None) -> Verdict:
    """A Verdict with only the fields that reach `__str__` varied."""
    return Verdict(
        intent_id="i1",
        source="danmaku",
        outcome=outcome,
        phase=phase,
        reason=reason,
    )


# ------------------------------------------------------------ rendering


def test_verdict_str_pairs_outcome_and_phase() -> None:
    assert str(_verdict(Outcome.SPOKEN, Phase.PLAYED)) == "spoken@played"


@pytest.mark.parametrize(
    ("outcome", "phase", "rendered"),
    [
        (Outcome.SKIPPED, Phase.GATED, "skipped@gated"),
        (Outcome.CANCELLED, Phase.SPEAKING, "cancelled@speaking"),
        (Outcome.EXPIRED, Phase.QUEUED, "expired@queued"),
    ],
)
def test_the_pairs_named_in_the_module_docstring_render_as_written(
    outcome: Outcome, phase: Phase, rendered: str
) -> None:
    """Those three examples are the module's own documentation of the format.

    They are also what a support reply quotes, so the strings are an interface.
    """
    assert str(_verdict(outcome, phase)) == rendered


def test_str_appends_the_reason_only_when_there_is_one() -> None:
    assert str(_verdict(Outcome.SKIPPED, Phase.GATED, SkipReason.COOLDOWN)) == (
        "skipped@gated(gate.cooldown)"
    )
    assert "(" not in str(_verdict(Outcome.SKIPPED, Phase.GATED))


def test_str_carries_nothing_but_the_pair_and_the_reason() -> None:
    """The other fields are separate columns in the panel, not part of the label.

    `detail` in particular can hold viewer text, which must not ride along into a
    string that gets logged and aggregated.
    """
    v = Verdict(
        intent_id="i1",
        source="danmaku",
        outcome=Outcome.FAILED,
        phase=Phase.GENERATING,
        detail="谢谢老板的舰长",
        waited_s=3.5,
        spoken_ms=1200,
    )
    assert str(v) == "failed@generating"


# ------------------------------------------------------------ immutability


def test_verdict_is_immutable() -> None:
    """A Verdict is a terminal record. Nothing downstream may edit one."""
    v = _verdict(Outcome.SPOKEN, Phase.PLAYED)

    with pytest.raises(FrozenInstanceError):
        v.intent_id = "i2"  # type: ignore[misc]

    # slots=True, so there is nowhere to put an attribute that is not declared.
    assert not hasattr(v, "__dict__")
    # TypeError, not AttributeError: the generated __setattr__ closes over the
    # pre-slots class, so an undeclared name falls through to a super() call that
    # cannot bind (CPython 3.12 dataclasses.py:639-646). Either way it does not
    # land, which is the part that matters here.
    with pytest.raises((TypeError, AttributeError)):
        v.retry_count = 1  # type: ignore[attr-defined]


# ------------------------------------------------------------ vocabulary


def test_skip_reason_vocabulary_is_pinned() -> None:
    """The append-only promise in SkipReason's docstring, enforced.

    Append-only means exactly this: adding a member leaves it green, while
    renaming or dropping one — which silently splits a stats bucket in two, or
    empties it — turns it red.
    """
    current = {m.name: m.value for m in SkipReason}
    changed = {
        name: current.get(name)
        for name, value in SKIP_REASONS.items()
        if current.get(name) != value
    }
    assert not changed, f"these reasons were renamed or removed: {changed}"

    # __members__ also lists aliases; iteration does not. Two names sharing a
    # value would merge two reasons into one bucket without any other symptom.
    assert len(SkipReason.__members__) == len(list(SkipReason))


def test_every_skip_reason_is_namespaced() -> None:
    """Stats aggregate by the prefix, so every value needs exactly one dot."""
    for reason in SkipReason:
        namespace, dot, name = reason.value.partition(".")
        assert dot, f"{reason.name} has no namespace prefix"
        assert namespace and name, f"{reason.name} has an empty half"
        assert "." not in name, f"{reason.name} has more than one level"


def test_outcome_and_phase_vocabularies_match_the_plan() -> None:
    """Plan §4.12 lists both sets literally, in this order.

    Unlike SkipReason these are closed: the control panel switches on them and
    shows the phases as a pipeline, so extending either one is a UI decision and
    should take a deliberate edit here.
    """
    assert [m.value for m in Outcome] == [
        "spoken",
        "skipped",
        "cancelled",
        "failed",
        "expired",
        "timed_out",
    ]
    assert [m.value for m in Phase] == [
        "selected",
        "queued",
        "gated",
        "dispatched",
        "generating",
        "speaking",
        "played",
    ]


def test_a_stored_reason_string_round_trips_and_a_typo_does_not() -> None:
    """Error path. Aggregation reads these back from logs and from the database,
    so an unrecognised string has to fail loudly instead of becoming a new bucket."""
    assert SkipReason("gate.cooldown") is SkipReason.COOLDOWN
    assert Outcome("skipped") is Outcome.SKIPPED
    assert Phase("gated") is Phase.GATED

    with pytest.raises(ValueError):
        SkipReason("gate.cooldwon")
    with pytest.raises(ValueError):
        Outcome("COOLDOWN")
