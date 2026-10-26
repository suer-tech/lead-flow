from contextlib import closing
from copy import deepcopy
from datetime import datetime,timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from worker.engine import Engine
from worker.reconsider import seed
from worker.store import Store
from worker.tests.test_research import FakeRuntime


class ReconsiderTests(unittest.TestCase):
    def test_budget_interrupted_prospect_reuses_read_pages_in_new_bounded_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            config={'campaign':'test','database':str(Path(tmp)/'research.sqlite'),'queries':['Relevant company research'],
                'max_candidates':12,'max_pages':72,'max_model_calls':1,'max_signal_age_days':180,'timezone':'UTC','mode':'preview','enabled':False}
            store=Store(config['database'])
            try:
                original=Engine(config,store,FakeRuntime()).run(slot='original')['runs'][0]['id']
                self.assertEqual('model_budget_exhausted',store.run(original)['error'])
                before=deepcopy(store.rows('SELECT * FROM candidates WHERE run_id=?',(original,)))
                config.update(discovery_outreach=True,max_model_calls=100)
                ident=seed(store,config,original)
                runtime=FakeRuntime()
                runtime.search=Mock(side_effect=AssertionError('No repeated discovery'))
                runtime.fetch=Mock(side_effect=AssertionError('Already read'))
                self.assertTrue(Engine(config,store,runtime).run(slot='reconsider-'+original)['ok'])
                self.assertEqual(4,store.run(ident)['model_calls'])
                self.assertEqual(before,store.rows('SELECT * FROM candidates WHERE run_id=?',(original,)))
                runtime.search.assert_not_called();runtime.fetch.assert_not_called()
            finally:store.close()

    def test_new_review_uses_saved_sources_preserves_parent_and_never_repeats_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            config={'campaign':'test','database':str(Path(tmp)/'research.sqlite'),'queries':['Relevant company research'],
                'max_candidates':12,'max_pages':72,'max_model_calls':70,'max_signal_age_days':180,'timezone':'UTC','mode':'preview','enabled':False}
            store=Store(config['database'])
            try:
                runtime=FakeRuntime();runtime.review['process_quote_supports_workflow']=False
                runtime.assessment.update(current_unmet_need=False,signal_kind='profile_process',signal_date='')
                original=Engine(config,store,runtime).run(slot='original')['runs'][0]['id']
                before=deepcopy(store.rows('SELECT * FROM candidates WHERE run_id=?',(original,)))
                self.assertEqual('rejected',before[0]['stage'])
                config['discovery_outreach']=True
                new=seed(store,config,original)
                rerun=FakeRuntime();rerun.assessment=runtime.assessment;rerun.review=runtime.review
                for method in ['search','fetch','followup','assess','repair_quotes']:
                    setattr(rerun,method,Mock(side_effect=AssertionError('Only new critic and letters')))
                result=Engine(config,store,rerun).run(slot='reconsider-'+original)
                self.assertTrue(result['ok'])
                candidate=store.rows('SELECT * FROM candidates WHERE run_id=?',(new,))[0]
                self.assertEqual('contact_ready',json.loads(candidate['result'])['lead']['qualification_status'])
                self.assertEqual(3,store.run(new)['model_calls'])
                self.assertEqual(before,store.rows('SELECT * FROM candidates WHERE run_id=?',(original,)))
                self.assertEqual(new,seed(store,config,original))
                self.assertEqual(2,len(store.rows('SELECT * FROM runs')))
                for method in ['search','fetch','followup','assess','repair_quotes']:getattr(rerun,method).assert_not_called()
            finally:store.close()
