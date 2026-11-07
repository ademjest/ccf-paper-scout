import copy
import io
import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from unittest import mock

import paper_scout
from ccf_paper_scout import cli, interests, sources
from scripts.build_actions_config import build
from state_store import StateStore
from test_sources import public_paper

ROOT = Path(__file__).resolve().parents[1]


class ManualInterestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root / 'config.json'
        self.base = json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8'))

    def write_config(self, **fields):
        self.base.update(fields)
        self.path.write_text(json.dumps(self.base), encoding='utf-8')
        return self.path

    def actions_config(self, env, smtp=False):
        with mock.patch.dict(os.environ, env, clear=True):
            build(ROOT / 'config.example.json', self.path, self.root / 'state', 5, smtp)
        return cli.load_config(self.path)

    def test_manual_preview_builds_without_zotero_smtp_or_llm(self):
        config = self.actions_config({'INTEREST_SOURCE':'manual', 'RESEARCH_PROFILE':'custom',
                                      'RELATION_MODE':'off'})
        self.assertEqual(config['interest_source'], 'manual')
        self.assertIn('document retrieval', config['explicit_interests'])
        self.assertFalse(config['llm_translation']['enabled'])
        self.assertFalse(config['relation_policy']['enabled'])
        self.assertFalse(config['relation_policy']['review']['enabled'])
        self.assertFalse(config['delivery']['smtp']['enabled'])
        self.assertNotIn('reinforcement learning', json.dumps(config['topic_priority']))
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.doctor(self.path, network=False), 0)

    def test_missing_manual_keywords_rejected_before_network(self):
        for value in ([], [''], 'world models', [None]):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cli.load_config(self.write_config(interest_source='manual', explicit_interests=value))
        with self.assertRaises(ValueError):
            cli.load_config(self.write_config(interest_source='typo'))

    def test_profile_resolves_relative_to_config_and_replaces_old_directions(self):
        profile = self.root / 'my-interests.json'
        profile.write_text(json.dumps({'explicit_interests':['document retrieval']}), encoding='utf-8-sig')
        config = cli.load_config(self.write_config(interest_source='manual', research_profile_file=profile.name))
        self.assertEqual(config['discovery_queries'], ['document retrieval'])
        self.assertEqual(config['topic_priority']['primary_topics'], ['document retrieval'])
        self.assertEqual(config['topic_priority']['exploration_topics'], [])
        self.assertEqual(config['research_profile']['directions'][0]['description'], 'document retrieval')
        self.assertNotIn('research_profile_file', config)

    def test_invalid_profile_does_not_fall_back_to_default_interests(self):
        profile = self.root / 'profile.json'
        for fields in ({'explicit_interests':[]}, {'explicit_interests':['retrieval'], 'discovery_queries':[]},
                       {'explicit_interests':['retrieval'], 'topic_priority':[]},
                       {'explicit_interests':['retrieval'], 'research_profile':{'directions':[{'name':'x'}]}}):
            profile.write_text(json.dumps(fields), encoding='utf-8')
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                interests.configure({'research_profile_file':str(profile)}, self.root)
        with self.assertRaises(FileNotFoundError):
            interests.configure({'research_profile_file':'missing.json'}, self.root)

    def test_zotero_still_requires_credentials_when_mode_is_omitted(self):
        self.base.pop('interest_source', None)
        self.write_config()
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.doctor(self.path, network=False), 2)
        with mock.patch.dict(os.environ, {'ZOTERO_USER_ID':'1', 'ZOTERO_API_KEY':'synthetic'}, clear=True), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.doctor(self.path, network=False), 0)

    def test_llm_opt_in_and_explicit_disable_override_legacy_credentials(self):
        old = {'LLM_BASE_URL':'https://example.org/v1', 'LLM_MODEL':'test'}
        config = self.actions_config(old)
        self.assertTrue(config['llm_translation']['enabled'])
        config = self.actions_config({**old, 'LLM_ENABLED':'false'})
        self.assertFalse(config['llm_translation']['enabled'])
        self.assertFalse(config['relation_policy']['review']['enabled'])
        with self.assertRaisesRegex(RuntimeError, 'LLM_BASE_URL'):
            self.actions_config({'LLM_ENABLED':'true'})

    def test_doctor_checks_llm_only_when_enabled(self):
        env = {'INTEREST_SOURCE':'manual', 'LLM_ENABLED':'true', 'LLM_BASE_URL':'https://example.org/v1', 'LLM_MODEL':'test'}
        self.actions_config(env)
        with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.doctor(self.path, network=False), 2)
        with mock.patch.dict(os.environ, {'LLM_API_KEY':'synthetic'}, clear=True), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.doctor(self.path, network=False), 0)

    def test_smtp_is_required_for_delivery_but_not_preview(self):
        with self.assertRaisesRegex(RuntimeError, 'SMTP_SENDER'):
            self.actions_config({'INTEREST_SOURCE':'manual'}, smtp=True)
        config = self.actions_config({'INTEREST_SOURCE':'manual', 'SMTP_SENDER':'sender@example.org',
            'SMTP_RECEIVER':'reader@example.org', 'SMTP_HOST':'smtp.example.org', 'SMTP_USE_SSL':'false'}, smtp=True)
        smtp = config['delivery']['smtp']
        self.assertEqual((smtp['host'], smtp['port'], smtp['use_ssl']), ('smtp.example.org', 587, False))
        with mock.patch.dict(os.environ, {'SMTP_PASSWORD':'synthetic-secret'}, clear=True), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.doctor(self.path, network=False), 0)
        self.assertNotIn('synthetic-secret', self.path.read_text(encoding='utf-8'))
        legacy = self.actions_config({'SMTP_SENDER':'s@example.org','SMTP_RECEIVER':'r@example.org'}, smtp=True)
        self.assertEqual((legacy['delivery']['smtp']['host'], legacy['delivery']['smtp']['port']), ('smtp.qq.com',465))

    def test_bad_environment_options_fail_early(self):
        for env in ({'LLM_ENABLED':'maybe'}, {'SMTP_USE_SSL':'no'}, {'SMTP_PORT':'0'},
                    {'SMTP_PORT':'65536'}, {'INTEREST_SOURCE':'auto'}, {'RESEARCH_PROFILE':'../../private'},
                    {'RELATION_MODE':'invalid'}):
            with self.subTest(env=env), self.assertRaises(ValueError):
                self.actions_config(env)

    def test_manual_workflow_uses_repository_settings_unless_explicitly_overridden(self):
        workflow = (ROOT/'.github/workflows/paper-scout-manual.yml').read_text(encoding='utf-8')
        fields = {'INTEREST_SOURCE':'interest_source', 'RESEARCH_PROFILE':'research_profile',
                  'SEMANTIC_MODE':'semantic_mode', 'RELATION_MODE':'relation_mode'}
        values = {'INTEREST_SOURCE':'manual', 'RESEARCH_PROFILE':'custom', 'SEMANTIC_MODE':'off', 'RELATION_MODE':'off'}
        for variable, field in fields.items():
            expression = re.search(r'^          '+variable+r': \$\{\{ (.+) \}\}$', workflow, re.MULTILINE)[1]
            expression = expression.replace('&&',' and ').replace('||',' or ')
            for choice, expected in [('configured',values[variable]), ('zotero' if field=='interest_source' else 'shadow',
                                                                       'zotero' if field=='interest_source' else 'shadow')]:
                context = dict(inputs=SimpleNamespace(**{field:choice}), vars=SimpleNamespace(**values))
                self.assertEqual(eval(expression, {'__builtins__':{}}, context), expected)
            if variable == 'INTEREST_SOURCE':
                context = dict(inputs=SimpleNamespace(interest_source='configured'), vars=SimpleNamespace(INTEREST_SOURCE=''))
                self.assertEqual(eval(expression, {'__builtins__':{}}, context), '')

    def test_manual_pipeline_recommends_custom_topic_without_external_credentials(self):
        self.actions_config({'INTEREST_SOURCE':'manual', 'RESEARCH_PROFILE':'custom', 'RELATION_MODE':'off'})
        config = json.loads(self.path.read_text(encoding='utf-8'))
        config.update(year_policy={'mode':'fixed'}, years=[2025], venue_keys=['icml'])
        self.path.write_text(json.dumps(config), encoding='utf-8')
        store = StateStore(Path(config['state_db']))
        paper = public_paper()
        paper.update(title='Document retrieval for grounded question answering',
                     abstract='We study document retrieval for question answering. Evidence passages are selected and reranked to support factual responses and reduce unsupported claims.')
        identifier = store.save_source_page('pmlr',dict(task_key='seed', records=[paper],state={}))[0]
        store.close()
        original = Path(config['state_db']).read_bytes()
        output = self.root/'report.md'
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(paper_scout,'fetch_zotero',side_effect=AssertionError('Zotero must not be called')), \
             mock.patch.object(paper_scout,'post_json',side_effect=AssertionError('LLM must not be called')), \
             mock.patch.object(paper_scout,'send_email') as mail, \
             mock.patch.object(sources,'discover',return_value={'mode':'cached_only','providers':{}}):
            self.assertEqual(cli.main(['run','--config',str(self.path),'--output',str(output),'--no-update-seen']),0)
        diagnostic = json.loads(output.with_suffix('.diagnostics.json').read_text(encoding='utf-8'))
        self.assertEqual(diagnostic['selected_ids'],[identifier])
        self.assertIn(paper['title'],output.read_text(encoding='utf-8'))
        self.assertEqual(Path(config['state_db']).read_bytes(), original)
        mail.assert_not_called()
        # Delivery history still excludes already delivered papers without a Zotero library.
        store = StateStore(Path(config['state_db']))
        run = store.start_run('manual-test')
        store.record_selection(run,[dict(paper,id=identifier)])
        store.finish_delivery(run,True)
        store.close()
        with mock.patch.dict(os.environ, {}, clear=True), \
             mock.patch.object(paper_scout,'fetch_zotero',side_effect=AssertionError('Zotero')), \
             mock.patch.object(paper_scout,'send_email') as mail:
            self.assertEqual(cli.main(['evaluate','--config',str(self.path),'--output',str(output)]),0)
        self.assertEqual(json.loads(output.with_suffix('.diagnostics.json').read_text(encoding='utf-8'))['selected_ids'],[])
        mail.assert_not_called()

    def test_zotero_pipeline_still_loads_interest_and_dedup_library(self):
        args = mock.Mock(interests=None, discover_only=False)
        config = copy.deepcopy(self.base)
        with mock.patch.object(paper_scout,'fetch_zotero',return_value=[]) as zotero, \
             mock.patch.object(paper_scout,'load_json',side_effect=RuntimeError('stop after interests')):
            with self.assertRaisesRegex(RuntimeError,'stop after interests'):
                paper_scout.run_pipeline(args,config)
        self.assertEqual(zotero.call_count,2)


if __name__ == '__main__':
    unittest.main()
