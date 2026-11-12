import copy
from pathlib import Path
import tempfile
from concurrent.futures import ThreadPoolExecutor
import unittest

from quota_policy import QuotaPolicy, parse_snapshot


def payload(used=30, ordinary=True):
    bucket = {'limitId': 'codex', 'primary': {'usedPercent': used, 'windowDurationMins': 300, 'resetsAt': 2000},
              'secondary': {'usedPercent': 15, 'windowDurationMins': 10080, 'resetsAt': 9000},
              'spendControlReached': False, 'rateLimitReachedType': None,
              'credits': {'hasCredits': True, 'unlimited': True, 'balance': '100'}}
    return {'accountId': 'test-account', 'ordinaryUsageAllowed': ordinary,
            'rateLimits': copy.deepcopy(bucket), 'rateLimitsByLimitId': {'codex': bucket}}


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'policy.sqlite3'

    def tearDown(self):
        self.tmp.cleanup()

    def snapshot(self, used=30, observed=100, **kwargs):
        return parse_snapshot(payload(used, **kwargs), account_id='test-account', clock_epoch='boot-A',
                              required_limits={'codex': {300, 10080}}, observed_monotonic=observed)

    def test_full_account_bound_snapshot_uses_most_consumed_window(self):
        snapshot = self.snapshot(used=50)
        self.assertTrue(snapshot.valid)
        self.assertEqual(snapshot.remaining_percent, 50)
        self.assertTrue(snapshot.ordinary_allowed)

    def policy(self):
        return QuotaPolicy(self.path, account_id='test-account', clock_epoch='boot-A', thread_id='thread-1')

    def test_healthy_request_is_reserved_and_not_replayed(self):
        policy = self.policy()
        first = policy.admit('request-1', self.snapshot(), now=101, fallback_ready=True)
        self.assertEqual(first.route, 'openai')
        self.assertEqual(policy.admit('request-2', self.snapshot(), now=101, fallback_ready=True).reason, 'active_request')
        policy.settle('request-1', 'completed')
        self.assertEqual(policy.admit('request-1', self.snapshot(), now=102, fallback_ready=True).reason, 'request_already_recorded')

    def test_warning_must_be_durably_acknowledged_before_low_allowance_admission(self):
        policy = self.policy()
        warning = policy.admit('request-1', self.snapshot(used=80), now=101, fallback_ready=True)
        self.assertEqual(warning.route, 'pause')
        self.assertEqual(warning.reason, 'warning_pending')
        self.assertIn('credits', warning.warning['text'])
        reopened = self.policy()
        self.assertEqual(reopened.admit('request-1', self.snapshot(used=80), now=102, fallback_ready=True).warning['id'], warning.warning['id'])
        reopened.acknowledge_warning(warning.warning['id'], 'local-notifier-receipt-1')
        self.assertEqual(reopened.admit('request-1', self.snapshot(used=80), now=103, fallback_ready=True).route, 'openai')

    def admit_after_warning(self, policy, request_id, snapshot, now=101, fallback_ready=True, **kwargs):
        result = policy.admit(request_id, snapshot, now=now, fallback_ready=fallback_ready, **kwargs)
        if result.warning:
            policy.acknowledge_warning(result.warning['id'], 'notifier-delivered')
            result = policy.admit(request_id, snapshot, now=now, fallback_ready=fallback_ready, **kwargs)
        return result

    def test_switch_is_sticky_until_new_healthy_evidence_and_explicit_recovery_notice(self):
        policy = self.policy()
        self.assertEqual(self.admit_after_warning(policy, 'low', self.snapshot(90)).route, 'minimax')
        policy.settle('low', 'completed')
        self.assertEqual(self.admit_after_warning(policy, 'still-fallback', self.snapshot(20, observed=102), now=102).route, 'minimax')
        policy.settle('still-fallback', 'completed')
        # Passing a reset time or an older snapshot cannot recover primary routing.
        self.assertEqual(self.admit_after_warning(policy, 'older', self.snapshot(20, observed=100), now=102, request_recovery=True).route, 'minimax')
        policy.settle('older', 'completed')
        recovery = policy.admit('recover', self.snapshot(20, observed=103), now=103, fallback_ready=True, request_recovery=True)
        self.assertEqual(recovery.warning['kind'], 'recovery')
        policy.acknowledge_warning(recovery.warning['id'], 'recovery-toast-shown')
        self.assertEqual(policy.admit('recover', self.snapshot(20, observed=103), now=103, fallback_ready=True, request_recovery=True).route, 'openai')

    def test_unknown_snapshot_cannot_admit_openai_and_unhealthy_fallback_pauses(self):
        policy = self.policy()
        self.assertEqual(self.admit_after_warning(policy, 'unknown', None, fallback_ready=False).reason, 'fallback_unavailable_or_incompatible')
        self.assertEqual(policy.admit('unknown', None, now=102, fallback_ready=True).route, 'minimax')

    def test_invalid_snapshots_do_not_admit_openai_even_with_credits(self):
        mutations = [
            lambda p: p.update(accountId='other'),
            lambda p: p.update(ordinaryUsageAllowed=None),
            lambda p: p.update(ordinaryUsageAllowed=False),
            lambda p: p['rateLimitsByLimitId']['codex'].update(secondary=None),
            lambda p: p['rateLimitsByLimitId']['codex']['primary'].update(usedPercent=float('nan')),
            lambda p: p['rateLimitsByLimitId']['codex']['primary'].update(usedPercent=float('inf')),
            lambda p: p['rateLimitsByLimitId']['codex']['primary'].update(usedPercent=10**1000),
            lambda p: p['rateLimitsByLimitId']['codex']['primary'].update(usedPercent=-1),
            lambda p: p['rateLimitsByLimitId']['codex']['primary'].update(windowDurationMins=42),
            lambda p: p['rateLimitsByLimitId'].update(new_bucket=copy.deepcopy(p['rateLimits'])),
            lambda p: p['rateLimitsByLimitId']['codex'].update(spendControlReached=None),
        ]
        for n, mutate in enumerate(mutations):
            with self.subTest(case=n):
                data = payload()
                mutate(data)
                snap = parse_snapshot(data, account_id='test-account', clock_epoch='boot-A', required_limits={'codex': {300, 10080}}, observed_monotonic=100)
                policy = QuotaPolicy(Path(self.tmp.name)/f'case-{n}.db', account_id='test-account', clock_epoch='boot-A', thread_id='thread-1')
                self.assertEqual(self.admit_after_warning(policy, 'request', snap).route, 'minimax')

    def test_sparse_future_and_stale_snapshots_cannot_unlock(self):
        samples = [
            parse_snapshot(payload(), account_id='test-account', clock_epoch='boot-A', required_limits={'codex': {300,10080}}, observed_monotonic=100, full_read=False),
            self.snapshot(observed=200), self.snapshot(observed=0),
        ]
        for n, sample in enumerate(samples):
            policy = QuotaPolicy(Path(self.tmp.name)/f'time-{n}.db', account_id='test-account', clock_epoch='boot-A', thread_id='thread-1')
            self.assertEqual(self.admit_after_warning(policy, 'request', sample).route, 'minimax')

    def test_uncertain_outcome_blocks_new_work_and_replay_after_restart(self):
        policy = self.policy()
        self.assertEqual(policy.admit('first', self.snapshot(), now=101, fallback_ready=True).route, 'openai')
        policy.settle('first', 'uncertain')
        reopened = self.policy()
        self.assertEqual(reopened.admit('next', self.snapshot(), now=102, fallback_ready=True).reason, 'uncertain_request')
        reopened.reconcile('first', evidence_receipt='verified-provider-receipt')
        self.assertEqual(reopened.admit('first', self.snapshot(), now=102, fallback_ready=True).reason, 'request_already_recorded')
        self.assertEqual(reopened.admit('next', self.snapshot(), now=102, fallback_ready=True).route, 'openai')

    def test_two_concurrent_owners_cannot_both_admit(self):
        policies = [self.policy(), self.policy()]
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(p.admit, f'request-{i}', self.snapshot(), now=101, fallback_ready=True) for i,p in enumerate(policies)]
            routes = [f.result().route for f in futures]
        self.assertEqual(sorted(routes), ['openai', 'pause'])

    def test_failed_warning_delivery_never_authorizes_dispatch(self):
        policy = self.policy()
        result = policy.admit('request', self.snapshot(90), now=101, fallback_ready=True)
        with self.assertRaises(ValueError):
            policy.acknowledge_warning(result.warning['id'], '')
        self.assertEqual(policy.admit('request', self.snapshot(90), now=102, fallback_ready=True).reason, 'warning_pending')

    def test_storage_failure_and_invalid_clock_pause(self):
        policy = self.policy()
        self.assertEqual(policy.admit('r', self.snapshot(), now=float('nan'), fallback_ready=True).route, 'pause')
        self.path.unlink()
        self.path.mkdir()
        self.assertEqual(policy.admit('r', self.snapshot(), now=101, fallback_ready=True).reason, 'durable_storage_unavailable')

    def test_threshold_edges_and_secondary_window_are_applied(self):
        for n, used, route in [(1,79.9,'openai'),(2,80,'openai'),(3,89.9,'openai'),(4,90,'minimax')]:
            policy = QuotaPolicy(Path(self.tmp.name)/f'edge-{n}.db', account_id='test-account', clock_epoch='boot-A', thread_id='thread-1')
            self.assertEqual(self.admit_after_warning(policy,'r',self.snapshot(used)).route, route)
        data = payload(used=1)
        data['rateLimitsByLimitId']['codex']['secondary']['usedPercent'] = 99
        snap = parse_snapshot(data, account_id='test-account', clock_epoch='boot-A', required_limits={'codex': {300,10080}}, observed_monotonic=100)
        self.assertEqual(self.admit_after_warning(self.policy(),'secondary-low',snap).route, 'minimax')

    def test_reboot_epoch_requires_fresh_evidence_explicit_recovery_and_notice(self):
        before = QuotaPolicy(self.path, account_id='test-account', thread_id='thread-1', clock_epoch='boot-A')
        low = parse_snapshot(payload(90), account_id='test-account', required_limits={'codex': {300,10080}},
                             observed_monotonic=500000, clock_epoch='boot-A')
        self.assertEqual(self.admit_after_warning(before, 'before-reboot', low, now=500001).route, 'minimax')
        before.settle('before-reboot', 'completed')
        after = QuotaPolicy(self.path, account_id='test-account', thread_id='thread-1', clock_epoch='boot-B')
        fresh = parse_snapshot(payload(20), account_id='test-account', required_limits={'codex': {300,10080}},
                               observed_monotonic=10, clock_epoch='boot-B')
        self.assertEqual(self.admit_after_warning(after, 'without-explicit-recovery', fresh, now=11).route, 'minimax')
        after.settle('without-explicit-recovery', 'completed')
        old_epoch = parse_snapshot(payload(20), account_id='test-account', required_limits={'codex': {300,10080}},
                                   observed_monotonic=10, clock_epoch='boot-A')
        self.assertEqual(self.admit_after_warning(after, 'old-epoch', old_epoch, now=11, request_recovery=True).route, 'minimax')
        after.settle('old-epoch', 'completed')
        recovery = after.admit('new-boot-recovery', fresh, now=11, fallback_ready=True, request_recovery=True)
        self.assertEqual(recovery.warning['kind'], 'recovery')
        after.acknowledge_warning(recovery.warning['id'], 'new-boot-notice-delivered')
        self.assertEqual(after.admit('new-boot-recovery', fresh, now=11, fallback_ready=True, request_recovery=True).route, 'openai')

    def test_manual_fallback_is_sticky_and_requires_notice_even_with_healthy_quota(self):
        policy = self.policy()
        manual = policy.admit('manual', self.snapshot(), now=101, fallback_ready=True, manual_fallback=True)
        self.assertEqual(manual.route, 'pause')
        self.assertEqual(manual.warning['kind'], 'switch')
        self.assertIn('manual_model_selection', manual.warning['text'])
        policy.acknowledge_warning(manual.warning['id'], 'manual-switch-notice-delivered')
        admitted = policy.admit('manual', self.snapshot(), now=101, fallback_ready=True, manual_fallback=True)
        self.assertEqual(admitted.route, 'minimax')
        self.assertEqual(admitted.reason, 'manual_model_selection')
        policy.settle('manual', 'completed')
        self.assertEqual(policy.admit('following', self.snapshot(observed=102), now=102, fallback_ready=True).route, 'minimax')

    def test_manual_fallback_rejects_nonboolean_flags_and_conflicting_recovery(self):
        policy = self.policy()
        for invalid in (0, 1, 'false', None):
            with self.subTest(value=invalid):
                decision = policy.admit('r', self.snapshot(), now=101, fallback_ready=True, manual_fallback=invalid)
                self.assertEqual(decision.route, 'pause')
                self.assertEqual(decision.reason, 'invalid_admission_flags')
        conflict = policy.admit('r', self.snapshot(), now=101, fallback_ready=True,
                                manual_fallback=True, request_recovery=True)
        self.assertEqual(conflict.reason, 'conflicting_route_selection')
        self.assertEqual(policy.admit('r', self.snapshot(), now=101, fallback_ready=True, manual_fallback=False).route, 'openai')

    def test_manual_switch_cannot_bypass_uncertain_work(self):
        policy = self.policy()
        self.assertEqual(policy.admit('first', self.snapshot(), now=101, fallback_ready=True).route, 'openai')
        policy.settle('first', 'uncertain')
        manual = policy.admit('manual', self.snapshot(), now=102, fallback_ready=True, manual_fallback=True)
        self.assertEqual(manual.route, 'pause')
        self.assertEqual(manual.reason, 'uncertain_request')
        self.assertIsNone(manual.warning)


if __name__ == '__main__':
    unittest.main()
