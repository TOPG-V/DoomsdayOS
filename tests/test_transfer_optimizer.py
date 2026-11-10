import importlib, tempfile, unittest
from pathlib import Path
from unittest import mock

import transfer_optimizer as optim

class TransferOptimizationTests(unittest.TestCase):
    def test_parallel_auto_reacts_to_fast_peers(self):
        self.assertEqual(optim.torrent_parallelism(60,45),1)
        self.assertEqual(optim.torrent_parallelism(60,0,4),2)
        self.assertEqual(optim.torrent_parallelism(60,2,4),4)
        self.assertEqual(optim.torrent_parallelism(60,20,4),2)

    def test_budget_guardrails(self):
        self.assertEqual(optim.budget_mib_s(0),16)
        self.assertEqual(optim.budget_mib_s(1),4)
        self.assertEqual(optim.budget_mib_s(99999),160)
        self.assertEqual(optim.budget_mib_s(100),65)

    def test_rate_limit_accounts_bytes(self):
        time=[0.0]
        def now():return time[0]
        def sleep(t):time[0]+=t
        p=optim.RateLimiter(1,clock=now,sleep=sleep)
        p.account(2*optim.MIB)
        self.assertAlmostEqual(time[0],2)
        p.account(optim.MIB)
        self.assertAlmostEqual(time[0],3)

    def test_small_benchmark_cleans_up_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            result=optim.benchmark_write(tmp,bytes_total=4*optim.MIB)
            self.assertEqual(result['bytes'],4*optim.MIB)
            self.assertGreater(result['write_mib_s'],0)
            self.assertEqual(list(Path(tmp).iterdir()),[])

    def test_tune_and_read_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp)/'policy.json'
            with mock.patch.object(optim,'dataset_identity',return_value='tank/stage'), \
                 mock.patch.object(optim,'benchmark_write',return_value={'write_mib_s':52,'measured_at':100}):
                optim.tune(Path(tmp),policy_path=state)
                self.assertEqual(optim.get_budget(Path(tmp),policy_path=state),33)

if __name__=='__main__':unittest.main()
