"""Scene-style Drive recognition; providers are mocked, filenames are not."""
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from test_gdi_js import ROOT
from Backend.helper.metadata import entry
from Backend.helper.metadata.parse import parse_scene_name, is_media_extra
from Backend.helper.settings_manager import Settings, SettingsManager
from Backend.helper.scan_manager import GDriveScanManager


class FilenameTests(TestCase):
    def test_movie_year_resolution_and_sequel_numbers(self):
        for name, title, year, quality in [
            ('movie.2049.2160p.whatever.mkv','movie',2049,'2160p'),
            ('movie.returns.2099.2160p.whatever.mkv','movie returns',2099,'2160p'),
            ('Blade.Runner.2049.2017.2160p.mkv','Blade Runner 2049',2017,'2160p'),
            ('Spider-Man.2.2004.mkv','Spider-Man 2',2004,'Unknown'),
            ('Movie Name (2021).mkv','Movie Name',2021,'Unknown'),
            ('1917.2019.1080p.mkv','1917',2019,'1080p'),
        ]:
            with self.subTest(name=name):
                result=parse_scene_name(name)
                self.assertEqual((result['title'],result['year'],result['quality']),(title,year,quality))
                self.assertIsNone(result['episode'])
                self.assertIsNone(result['season'])

    def test_spaced_and_dotted_episode_names(self):
        for name in ('Show Name S01 E01.mkv','Show.Name.S01.E01.2160p.whatever.mkv','Show.Name.S01E01.mkv'):
            result=parse_scene_name(name)
            self.assertEqual((result['title'],result['season'],result['episode']),('Show Name',1,1))
        self.assertEqual(parse_scene_name('Show.S00E01.mkv')['season'],0)

    def test_featurettes_and_extras_are_not_main_movies(self):
        self.assertTrue(is_media_extra('Nice Shot, Floyd! The Greatest Marksman in the DCU - Featurette.mkv'))
        self.assertTrue(is_media_extra('Interview.mkv',ROOT+'Movie/Extras/Interview.mkv'))
        self.assertTrue(is_media_extra('Clip.mkv',ROOT+'Movie/Behind%20the%20Scenes/Clip.mkv'))
        self.assertFalse(is_media_extra('The.Extras.2019.1080p.mkv',ROOT+'Movies/The.Extras.2019.1080p.mkv'))
        self.assertFalse(is_media_extra('Movie.2020.mkv',ROOT+'Movies/Subfolder/Movie.2020.mkv'))


class RoutingTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings=patch.object(SettingsManager,'_current',Settings({}))
        self.settings.start();self.addCleanup(self.settings.stop)

    async def test_movie_without_resolution_is_matched_as_unknown(self):
        with patch.object(entry,'resolve_movie',AsyncMock(return_value={'title':'Spider-Man 2'})) as movie, \
             patch.object(entry,'resolve_series',AsyncMock()) as tv:
            result=await entry.metadata('Spider-Man.2.2004.mkv',0,0,scene_filename=True)
        self.assertIsNotNone(result)
        self.assertEqual(movie.call_args.args[0],'Spider-Man 2')
        self.assertEqual(movie.call_args.kwargs['year'],2004)
        self.assertEqual(movie.call_args.kwargs['quality'],'Unknown')
        tv.assert_not_awaited()

    async def test_number_in_movie_title_is_preserved(self):
        for scene in (True,False):
            with patch.object(entry,'resolve_movie',AsyncMock(return_value={'title':'Blade Runner 2049'})) as movie:
                await entry.metadata('Blade.Runner.2049.2017.2160p.mkv',0,0,scene_filename=scene)
            self.assertEqual(movie.call_args.args[0],'Blade Runner 2049')

    async def test_episode_without_resolution_goes_to_tv_and_preserves_specials(self):
        for season in (0,1):
            with patch.object(entry,'resolve_series',AsyncMock(return_value={'title':'Show Name'})) as tv, \
                 patch.object(entry,'resolve_movie',AsyncMock()) as movie:
                await entry.metadata(f'Show Name S{season:02d} E01.mkv',0,0,scene_filename=True)
            self.assertEqual(tv.call_args.args[:3],('Show Name',season,1))
            self.assertEqual(tv.call_args.kwargs['quality'],'Unknown')
            movie.assert_not_awaited()

    async def test_extras_have_separate_scan_counter(self):
        manager=GDriveScanManager()
        manager.bind_db(SimpleNamespace(dbs={},insert_media=AsyncMock()))
        async def files(settings):
            yield {'name':'Outback Rogue - Captain Boomerang - Featurette.mkv','url':ROOT+'extra.mkv'}
        with patch.object(manager,'_iter_files',files),patch('Backend.helper.scan_manager.metadata',AsyncMock()) as metadata:
            await manager._run()
        self.assertEqual(manager.state['counters']['skipped_extras'],1)
        self.assertEqual(manager.state['counters']['skipped_meta'],0)
        metadata.assert_not_awaited()
