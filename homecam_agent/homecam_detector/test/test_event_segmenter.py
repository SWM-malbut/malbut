"""Tests for event clip confirmation, merging, splitting, and privacy."""

from homecam_detector.event_segmenter import EventSegmenter, uuid7


SESSION_A = "22222222-2222-4222-8222-222222222222"
SESSION_B = "33333333-3333-4333-8333-333333333333"


def _segmenter(**overrides) -> EventSegmenter:
    options = {
        "confirmation_window_frames": 5,
        "confirmation_required_frames": 3,
        "pre_roll_sec": 5.0,
        "merge_gap_sec": 10.0,
        "max_segment_sec": 120.0,
        "boot_id": "11111111-1111-4111-8111-111111111111",
        "group_id_factory": lambda _timestamp: (
            "018f1e90-7b80-7000-8000-000000000001"
        ),
    }
    options.update(overrides)
    return EventSegmenter("device-1", **options)


def _observe(segmenter, candidates, wall, monotonic, session=SESSION_A):
    return segmenter.observe(
        candidates,
        occurred_at=wall,
        observed_at=monotonic,
        session_id=session,
    )


def test_uuid7_contains_time_version_and_variant() -> None:
    value = uuid7(1_700_000_000_000)
    assert value[14] == "7"
    assert value[19] in "89ab"


def test_three_of_five_confirms_despite_one_missed_frame() -> None:
    segmenter = _segmenter()
    assert _observe(segmenter, {"person": 0.7}, 100.0, 10.0) == []
    assert _observe(segmenter, {}, 100.1, 10.1) == []
    assert _observe(segmenter, {"person": 0.8}, 100.2, 10.2) == []
    boundaries = _observe(segmenter, {"person": 0.9}, 100.3, 10.3)
    assert [boundary.phase for boundary in boundaries] == ["started"]
    assert boundaries[0].start_at == 95.3


def test_repeated_detection_merges_labels_and_closes_after_gap() -> None:
    segmenter = _segmenter(
        confirmation_window_frames=1,
        confirmation_required_frames=1,
    )
    started = _observe(segmenter, {"motion": 1.0}, 100.0, 10.0)[0]
    assert _observe(segmenter, {"person": 0.9}, 105.0, 15.0) == []
    assert _observe(segmenter, {}, 114.9, 24.9) == []
    ended = _observe(segmenter, {}, 115.0, 25.0)[0]
    assert started.event_group_id == ended.event_group_id
    assert ended.labels == ("person", "motion")
    assert ended.primary_type == "person"
    assert ended.end_at == 115.0
    assert ended.monotonic_duration_ms == 20_000


def test_max_duration_splits_without_changing_event_group() -> None:
    segmenter = _segmenter(
        confirmation_window_frames=1,
        confirmation_required_frames=1,
        pre_roll_sec=0.0,
        max_segment_sec=4.0,
    )
    first = _observe(segmenter, {"person": 0.8}, 100.0, 10.0)[0]
    split = _observe(segmenter, {"person": 0.9}, 104.0, 14.0)
    assert [boundary.phase for boundary in split] == ["ended", "started"]
    assert split[0].segment_index == 0
    assert split[1].segment_index == 1
    assert first.event_group_id == split[1].event_group_id


def test_privacy_discard_emits_nothing_and_forgets_active_event() -> None:
    segmenter = _segmenter(
        confirmation_window_frames=1,
        confirmation_required_frames=1,
    )
    assert _observe(segmenter, {"person": 0.9}, 100.0, 10.0)
    segmenter.discard()
    assert not segmenter.active
    assert _observe(segmenter, {}, 200.0, 20.0) == []


def test_storage_session_ids_accumulate_across_refresh() -> None:
    segmenter = _segmenter(
        confirmation_window_frames=1,
        confirmation_required_frames=1,
        pre_roll_sec=0.0,
        merge_gap_sec=1.0,
    )
    started = _observe(segmenter, {"person": 0.8}, 100.0, 10.0)[0]
    _observe(segmenter, {"person": 0.9}, 101.0, 11.0, SESSION_B)
    ended = _observe(segmenter, {}, 102.0, 12.0, SESSION_B)[0]
    assert started.session_ids == (SESSION_A,)
    assert ended.session_ids == (SESSION_A, SESSION_B)


def test_notification_cooldown_does_not_block_second_clip() -> None:
    segmenter = _segmenter(
        confirmation_window_frames=1,
        confirmation_required_frames=1,
        pre_roll_sec=0.0,
        merge_gap_sec=1.0,
        notification_cooldown_sec=30.0,
    )
    first = _observe(segmenter, {"person": 0.8}, 100.0, 10.0)[0]
    _observe(segmenter, {}, 101.0, 11.0)
    second = _observe(segmenter, {"person": 0.9}, 105.0, 15.0)[0]
    assert first.notification_eligible
    assert not second.notification_eligible
    assert first.event_group_id != ""
