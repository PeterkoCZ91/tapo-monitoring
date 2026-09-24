from tapo_monitor import motion


def test_nothing_in_the_way_allows_both_paths():
    assert motion.decide(motion.SCHEDULE).allowed
    assert motion.decide(motion.GUARD).allowed


def test_privacy_refuses_every_requester():
    for requester in (motion.SCHEDULE, motion.GUARD):
        d = motion.decide(requester, privacy_on=True, hold=True, autotrack_on=True,
                          out_of_bounds_for=999, hold_grace=20)
        assert d == motion.Decision(False, motion.PRIVACY)


def test_hold_refuses_the_scheduled_recall_while_tracking():
    d = motion.decide(motion.SCHEDULE, hold=True, autotrack_on=True)
    assert d == motion.Decision(False, motion.HOLD)


def test_hold_is_ignored_without_tracking():
    # By day the recall is the only thing that repairs a nudged aim.
    assert motion.decide(motion.SCHEDULE, hold=True, autotrack_on=False).allowed
    assert motion.decide(motion.GUARD, hold=True, autotrack_on=False).allowed


def test_guard_waits_out_the_grace_against_a_hold():
    early = motion.decide(motion.GUARD, hold=True, autotrack_on=True,
                          out_of_bounds_for=6, hold_grace=20)
    late = motion.decide(motion.GUARD, hold=True, autotrack_on=True,
                         out_of_bounds_for=20, hold_grace=20)
    assert early == motion.Decision(False, motion.HOLD)
    assert late.allowed


def test_zero_grace_restores_the_guard_overriding_any_hold():
    assert motion.decide(motion.GUARD, hold=True, autotrack_on=True,
                         out_of_bounds_for=0, hold_grace=0).allowed


def test_count_refusal_buckets_per_camera_and_reason():
    counters = {}
    motion.count_refusal(counters, "a", motion.GUARD, motion.HOLD)
    motion.count_refusal(counters, "a", motion.GUARD, motion.HOLD)
    motion.count_refusal(counters, "b", motion.SCHEDULE, motion.PRIVACY)
    assert counters == {"a": {"pan_limit:hold": 2}, "b": {"schedule:privacy": 1}}
