"""Review fixes and durable page-boundary scan progress."""
import asyncio
import copy
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, patch

from test_gdi_js import ROOT
from Backend.helper import gdi_js as gdi
from Backend.helper.scan_manager import GDriveScanManager
from Backend.helper.settings_manager import Settings, SettingsManager


class FinalizationTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = Settings({'gdrive_source_type': 'gdi_js', 'gdrive_index_url': ROOT,
                                  'gdrive_index_password': 'saved-secret',
                                  'gdrive_selected_folders': ['/0:/Movies/']})
        self.current = patch.object(SettingsManager, '_current', self.settings)
        self.current.start(); self.addCleanup(self.current.stop)

    async def test_webdav_propagates_gdi_kind(self):
        from Backend.fastapi.routes import webdav_routes, stream_routes
        node = SimpleNamespace(stream_id='stored-id', stream_name='Movie.mp4')
        with patch.object(webdav_routes, 'decode_string', AsyncMock(return_value={
                'source':'gdrive','kind':'gdi_js','url':ROOT+'Movies/Movie.mp4'})), \
             patch.object(stream_routes, 'gdrive_media_streamer', AsyncMock()) as stream:
            await webdav_routes._stream_video(None,node,'token',{})
        self.assertEqual(stream.call_args.kwargs['source_kind'],'gdi_js')
        self.assertEqual(stream.call_args.kwargs['stream_id_hash'],'stored-id')

    async def test_implicit_kind_keeps_indexed_media_gate(self):
        from Backend.fastapi.routes import stream_routes
        from fastapi import HTTPException
        with patch.object(stream_routes,'decode_string',AsyncMock(return_value={'kind':'gdi_js'})), \
             patch.object(stream_routes.db,'is_indexed_gdrive_stream',AsyncMock(return_value=False)), \
             patch.object(stream_routes,'remote_media_response',AsyncMock()) as stream:
            with self.assertRaises(HTTPException) as error:
                await stream_routes.gdrive_media_streamer(None,ROOT+'private.mp4','private.mp4','token',stream_id_hash='forged')
        self.assertEqual(error.exception.status_code,404)
        stream.assert_not_awaited()

    async def test_backup_new_origin_clears_password_same_origin_keeps_it(self):
        from Backend.helper import backup
        for url, clears in [('https://new.example/0:/', True), (ROOT, False)]:
            with patch.object(SettingsManager,'update',AsyncMock(return_value={})) as update:
                await backup.import_config({'app':'telegram-stremio','settings':{
                    'gdrive_source_type':'gdi_js','gdrive_index_url':url,'gdrive_selected_folders':[]}})
            clean=update.call_args.args[1]
            self.assertEqual('gdrive_index_password' in clean,clears)
            if clears:self.assertEqual(clean['gdrive_index_password'],'')

    def fake_database(self):
        self.saved={}
        async def update(query,changes,**kwargs):self.saved.update(copy.deepcopy(changes['$set']))
        state=SimpleNamespace(update_one=AsyncMock(side_effect=update),find_one=AsyncMock(side_effect=lambda q:copy.deepcopy(self.saved)))
        return SimpleNamespace(dbs={'tracking':{'state':state}},insert_media=AsyncMock())

    async def test_page_is_committed_only_after_indexing_and_restored(self):
        database=self.fake_database()
        async def pages(*args,**kwargs):
            yield [{'name':'Movie.mp4','url':ROOT+'Movies/Movie.mp4','kind':'gdi_js'}], {
                'queue':[],'token':None,'index':0,'tokens':[],'pages':1,'files':1}, '/0:/Movies/'
        with patch.object(gdi,'discover_pages',pages):
            manager=GDriveScanManager();manager.bind_db(database)
            iterator=manager._iter_files(self.settings)
            await anext(iterator)
            self.assertEqual(self.saved['cursor']['queue'],['/0:/Movies/'])
            await iterator.aclose()
            restarted=GDriveScanManager();restarted.bind_db(database)
            await restarted.restore_checkpoint()
            self.assertTrue(restarted.get_status()['resumable'])
            _=[item async for item in restarted._iter_files(self.settings)]
            self.assertEqual(self.saved['cursor']['queue'],[])
            self.assertFalse(restarted.get_status()['resumable'])

    async def test_page_failure_keeps_cursor_for_retry(self):
        database=self.fake_database();manager=GDriveScanManager();manager.bind_db(database)
        async def pages(*args,**kwargs):
            yield [{'name':'Movie.mp4'}], {'queue':[],'pages':1,'files':1}, '/0:/Movies/'
        with patch.object(gdi,'discover_pages',pages):
            iterator=manager._iter_files(self.settings)
            await anext(iterator)
            manager.state['counters']['errors']+=1
            with self.assertRaises(gdi.GDIError):await anext(iterator)
        self.assertEqual(self.saved['cursor']['queue'],['/0:/Movies/'])

    async def test_changed_selection_invalidates_checkpoint(self):
        database=self.fake_database();manager=GDriveScanManager();manager.bind_db(database)
        await manager._save_cursor({'queue':['/0:/Movies/']})
        with patch.object(SettingsManager,'_current',Settings({**self.settings.to_dict(),'gdrive_selected_folders':['/0:/Shows/']})):
            await manager.restore_checkpoint()
            self.assertFalse(manager.get_status()['resumable'])

    async def test_resume_keeps_rescan_mode_and_explicit_rescan_restarts(self):
        database=self.fake_database();manager=GDriveScanManager();manager.bind_db(database)
        manager.state['mode']='rescan'
        await manager._save_cursor({'queue':['/0:/Movies/'],'index':2})
        with patch.object(manager,'_run',AsyncMock()):
            await manager.start('scan')
            self.assertEqual(manager.state['mode'],'rescan')
            self.assertTrue(manager._resume_requested)
            await manager._task
            manager.state['status']='cancelled'
            await manager.start('rescan')
            self.assertFalse(manager._resume_requested)
            await manager._task

    async def test_stop_interrupts_network_wait(self):
        manager=GDriveScanManager();manager.state['status']='running'
        manager._task=asyncio.create_task(asyncio.Event().wait())
        await manager.cancel()
        self.assertEqual(manager.state['status'],'cancelled')
        self.assertTrue(manager._task.done())

    async def test_page_resume_passes_exact_cursor_and_filters_subfolders(self):
        cursor={'queue':['/0:/Movies/'],'token':'next','index':2,'tokens':['next'],'pages':2,'files':5}
        listing={'items':[{'name':'Skip','path':'/0:/Movies/Skip/','is_folder':True},
                          {'name':'New.mp4','path':'/0:/Movies/New.mp4','is_folder':False}], 'next_page_token':None}
        with patch.object(gdi.GDIClient,'list_page',AsyncMock(return_value=listing)) as request:
            pages=[p async for p in gdi.discover_pages(gdi.GDIConfig(ROOT),['/0:/Movies/'],excludes=['skip'],checkpoint=cursor)]
        request.assert_awaited_once_with('/0:/Movies/','next',2)
        self.assertEqual(pages[0][1]['queue'],[])
        self.assertEqual(pages[0][1]['files'],6)
        self.assertEqual(pages[0][0][0]['url'],ROOT+'Movies/New.mp4')
