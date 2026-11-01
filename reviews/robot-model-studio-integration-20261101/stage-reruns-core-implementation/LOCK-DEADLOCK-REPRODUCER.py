from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from unittest.mock import patch
import copy
import json
import uuid

from description_pipeline.io import digest
from description_pipeline.pipeline.admission import AdmissionSpec, build_publish_command
from description_pipeline.pipeline.ingestion import ProductionAdmissionFactory, ProductionStageRerunRequest
from description_pipeline.platform.facts import FactStore
from tests.v1.stage_rerun_child import FixtureDispatch
from tests.v1.test_real_six_stage_chain import RealSixStageChainTests

RealSixStageChainTests.setUpClass()
f = RealSixStageChainTests(methodName='runTest')
try:
    f.setUp()
    work = f.root / 'lock-reproduction'
    work.mkdir()
    app = f._application()
    factory = ProductionAdmissionFactory(artifacts=f.store, platform_snapshot=f._platform_snapshot(),
        native_tool_provider=lambda: copy.deepcopy(f.native), portable_tool_provider=lambda: copy.deepcopy(f.portable),
        work_root=work, clock=lambda: datetime(2026, 10, 25, tzinfo=UTC))
    with ExitStack() as stack:
        for target, tool in (('description_pipeline.runtime.tool_record',f.native),
                             ('description_pipeline.pipeline.stages.generate.tool_record',f.portable),
                             ('description_pipeline.pipeline.stages.publish.tool_record',f.portable)):
            stack.enter_context(patch(target,return_value=copy.deepcopy(tool)))
        source = factory.prepare(f._production_request()).spec
        original = app.admit(source)
        app.dispatch(original.dispatch_command.command_id,FixtureDispatch())
        assert app.execute_run(source.run_id).outcome == 'completed'
        middle = factory.admit_stage_rerun(app,ProductionStageRerunRequest(run_id=str(uuid.uuid4()),request_id='middle',
            parent_run_id=source.run_id,principal=source.principal,change_summary='Recompute offline stages',
            from_stage='generate',through_stage='publish'))
        app.dispatch(middle.dispatch_command.command_id,FixtureDispatch())
        assert app.execute_run(middle.admission.run_id,through='mjcf').outcome == 'completed'
        spec = AdmissionSpec.from_record(middle.admission.request)
        prefix = app.source_context(spec)[0]
        # Read executed middle facts while preserving its real reused prefix.
        complete, _, verified = app._read_source_prefix(spec,6,frozenset())
        publisher = build_publish_command(spec,verified,spec.publish_intent,complete['mjcf'])
        f.facts.create_command(publisher,admission=middle.admission,predecessor=complete['mjcf'],
            supporting_predecessors=(complete['verify'],),verify_result=verified,publish_intent=spec.publish_intent)
        child = factory.admit_stage_rerun(app,ProductionStageRerunRequest(run_id=str(uuid.uuid4()),request_id='child',
            parent_run_id=spec.run_id,principal=source.principal,change_summary='Recompute native discovery',from_stage='discover'))
        barrier = Barrier(2)
        facts = {name: FactStore(f._connect,f.store) for name in ('publisher','discover')}
        first = {'publisher':source.run_id,'discover':spec.run_id}
        fired = set()
        orders = {name:[] for name in facts}
        for name, store in facts.items():
            raw = store._lock_run_cursor
            def locked(cursor, run_id, *, name=name, raw=raw):
                raw(cursor,run_id)
                orders[name].append(run_id)
                if name not in fired and run_id == first[name]:
                    fired.add(name)
                    barrier.wait(timeout=10)
            stack.enter_context(patch.object(store,'_lock_run_cursor',side_effect=locked))
        def acquire(name,command):
            try:
                facts[name].acquire_claim(command.command_id,claim_id='lock-'+name,
                    owner_sha256=digest({'owner':name}),executor_sha256=command.expected_executor_sha256,
                    now=datetime(2026,10,25,tzinfo=UTC),expires_at=datetime(2026,10,25,tzinfo=UTC)+timedelta(minutes=5))
                return {'outcome':'acquired'}
            except Exception as error:
                return {'outcome':'error','type':type(error).__name__,'message':str(error)}
        with ThreadPoolExecutor(max_workers=2) as pool:
            left=pool.submit(acquire,'publisher',publisher)
            right=pool.submit(acquire,'discover',child.entry_command)
            results={'publisher':left.result(timeout=20),'discover':right.result(timeout=20)}
        print(json.dumps({'source':source.run_id,'middle':spec.run_id,'child':child.admission.run_id,'orders':orders,'results':results},indent=2))
finally:
    f.doCleanups()
    RealSixStageChainTests.tearDownClass()
