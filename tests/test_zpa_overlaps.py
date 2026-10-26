"""Explicit, preview-bound exceptions for narrower subnets under ZPA summaries."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from queue import SimpleQueue, Empty
import tempfile
import unittest
from unittest.mock import patch

from tests import test_zpa_segments as fixture
import zpa_segments as segments
from input_validation import validate_rows
from ui_deployment import run_worker, plan_digest


class OverlapTests(unittest.TestCase):
    setUp = fixture.SegmentTests.setUp
    tearDown = fixture.SegmentTests.tearDown
    engine = fixture.SegmentTests.engine

    def summary(self):
        value = dict(id='existing', name='Corporate summary', domainNames=['10.20.0.0/16'], enabled=True,
                     tcpPortRange=[{'from':'1','to':'65535'}], udpPortRange=[], serverGroups=[{'id':'existing-servers'}])
        self.api.data['application']['existing'] = value
        return value

    def reviewed(self, acknowledged=False):
        overlaps = segments.check_conflicts(self.plan, segments.load_inventory(fixture.CONTEXT), review_contained=True)
        return replace(self.plan, overlaps=tuple(overlaps), overlap_acknowledged=acknowledged)

    def test_default_blocks_but_review_returns_exact_containment(self):
        self.summary()
        with self.assertRaises(segments.SegmentConflict):segments.check_conflicts(self.plan,segments.load_inventory(fixture.CONTEXT))
        plan=self.reviewed()
        self.assertEqual(len(plan.overlaps),1)
        overlap=plan.overlaps[0]
        self.assertEqual((overlap['application_id'],overlap['existing_subnet'],overlap['new_subnet']),
                         ('existing','10.20.0.0/16','10.20.0.0/24'))
        self.assertFalse(plan.report()['enabled']);self.assertFalse(plan.overlap_acknowledged)

    def test_exact_narrower_wildcard_and_duplicate_resource_names_always_block(self):
        for destination in ('10.20.0.0/24','10.20.0.0/25','10.20.0.5','*'):
            self.summary()['domainNames']=[destination]
            with self.subTest(destination=destination),self.assertRaises(segments.SegmentConflict):self.reviewed(True)
        self.summary()['name']=self.plan.application_name
        with self.assertRaises(segments.SegmentConflict):self.reviewed(True)
        self.summary()
        self.api.data['serverGroup']['old']=dict(id='old',name=self.plan.server_group_name)
        with self.assertRaises(segments.SegmentConflict):self.reviewed(True)

    def test_multiple_existing_summaries_require_separate_confirmations(self):
        self.summary()
        self.api.data['application']['other']=dict(id='other',name='Wider summary',domainNames=['10.0.0.0/8'],enabled=False)
        plan=self.reviewed()
        self.assertEqual(len({r['confirmation_id'] for r in plan.overlaps}),2)
        other=segments.build_segment_plan('Other branch',self.validation.sites[0].vlans)
        with self.assertRaisesRegex(segments.SegmentConflict,'another selected site'):
            segments.check_batch_conflicts([plan,other],segments.load_inventory(fixture.CONTEXT),review_contained=True)

    def test_approved_staging_preserves_existing_segment_and_creates_disabled_application(self):
        original=deepcopy(self.summary())
        plan=self.reviewed(True);report=plan.report()
        self.assertTrue(segments.stage_segments(fixture.CONTEXT,plan,'new-connector',report,emit=lambda _:None))
        self.assertEqual(self.api.data['application']['existing'],original)
        self.assertTrue(report['overlap_acknowledged']);self.assertEqual(report['overlap_verification'],'confirmed')
        created=[c for c in self.api.calls if c[0]=='POST' and c[1]=='application']
        self.assertEqual(len(created),1);self.assertIs(created[0][2]['enabled'],False)
        self.assertEqual(created[0][2]['icmpAccessType'],'NONE')
        self.assertFalse(any(c[0]=='PUT' for c in self.api.calls))

    def test_staging_rechecks_acknowledgment_and_existing_configuration_before_writes(self):
        self.summary();plan=self.reviewed()
        with self.assertRaises(segments.SegmentConflict):
            segments.stage_segments(fixture.CONTEXT,plan,'connector',plan.report())
        for change in (dict(enabled=False),dict(domainNames=['10.0.0.0/8']),dict(tcpPortRange=[]),
                       dict(name='Renamed'),dict(serverGroups=[{'id':'different'}])):
            self.summary();approved=self.reviewed(True)
            self.api.data['application']['existing'].update(change)
            with self.subTest(change=change),self.assertRaises(segments.SegmentConflict):
                segments.stage_segments(fixture.CONTEXT,approved,'connector',approved.report())
        self.assertTrue(all(c[0]=='GET' for c in self.api.calls))

    def test_removed_or_new_overlap_invalidates_confirmation(self):
        self.summary();plan=self.reviewed(True)
        self.api.data['application'].clear()
        with self.assertRaises(segments.SegmentConflict):segments.stage_segments(fixture.CONTEXT,plan,'connector',plan.report())
        self.summary();self.api.data['application']['other']=dict(id='other',name='Other',domainNames=['10.0.0.0/8'])
        with self.assertRaises(segments.SegmentConflict):segments.stage_segments(fixture.CONTEXT,plan,'connector',plan.report())
        self.assertTrue(all(c[0]=='GET' for c in self.api.calls))

    def test_engine_never_executes_review_only_plan_or_cli_bypass_flag(self):
        self.summary();engine=self.engine()
        strict=engine.plan(validate_rows([fixture.site(zpa_allow_overlap='1')]))
        self.assertTrue(strict.issues)
        reviewed=engine.plan(self.validation,review_zpa_overlaps=True)
        self.assertFalse(reviewed.issues)
        self.assertEqual(engine.execute(reviewed,dry_run=True).exit_code,0)
        self.assertEqual(engine.execute(reviewed).exit_code,1)
        engine.create_site.assert_not_called()

    def worker(self,engine,directory,mode='preview',expected=None,confirmations=None):
        queue=SimpleQueue()
        with patch('ui_deployment.DeploymentEngine',return_value=engine),patch('ui_deployment.ignore_terminal_signals'),patch('ui_deployment.capture_results'):
            run_worker([fixture.site()],engine.config,mode,expected,directory,queue,overlap_confirmations=confirmations)
        events=[]
        while True:
            try:events.append(queue.get_nowait())
            except Empty:break
        return events[-1]

    def test_worker_requires_confirmation_and_persists_it_in_reports(self):
        self.summary();engine=self.engine()
        with tempfile.TemporaryDirectory() as directory:
            preview=self.worker(engine,directory)
            self.assertEqual(preview['state'],'ready')
            rejected=self.worker(engine,directory,'deployment',preview['plan_digest'])
            self.assertEqual(rejected['state'],'blocked');engine.create_site.assert_not_called()
            confirmations=[o['confirmation_id'] for o in preview['zpa_overlaps']]
            final=self.worker(engine,directory,'deployment',preview['plan_digest'],confirmations)
            self.assertEqual(final['state'],'completed',final)
            report=json.loads((Path(directory)/final['report']).read_text())
            self.assertTrue(report['sites'][0]['zpa_segments']['overlap_acknowledged'])
            text=(Path(directory)/final['report']).with_suffix('.txt').read_text()
            self.assertIn('ZPA summary overlap acknowledged',text)
            self.assertIn('10.20.0.0/16',text)
            self.assertNotIn(fixture.CONTEXT.token,text)

    def test_changed_summary_blocks_worker_before_site_creation(self):
        self.summary();engine=self.engine()
        with tempfile.TemporaryDirectory() as directory:
            preview=self.worker(engine,directory)
            self.api.data['application']['existing']['enabled']=False
            final=self.worker(engine,directory,'deployment',preview['plan_digest'],[o['confirmation_id'] for o in preview['zpa_overlaps']])
            self.assertEqual(final['state'],'blocked')
            engine.create_site.assert_not_called()
            self.assertTrue(all(c[0]=='GET' for c in self.api.calls))
