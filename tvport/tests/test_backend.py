import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backend  # noqa: E402

LOG = '''[dm] [nvn-mtl] memory up to frame 2700: resident 1867 MB, GPU allocated 1483 MB, deferred-release queue 10; headroom to the system memory limit 230 MB
[longframe] frame 1491 (27.11 s) whole frame 556.1 ms, submit+present 533.1 ms, GPU 238.4 ms; new pipelines 0 + shader libraries 0, 0.0 ms in all; texture uploads 264 first + 0 re-uploads, 61.4 MB / 284.6 ms; draws with a hybrid pipeline 0
[longframe] frame 1632 (29.47 s) whole frame 54.5 ms, submit+present 40.5 ms, GPU 9.5 ms; new pipelines 4 + shader libraries 0, 31.8 ms in all; texture uploads 0 first + 0 re-uploads, 0.0 MB / 0.0 ms; draws with a hybrid pipeline 0
[longframe] frame 1535 (27.84 s) whole frame 23.3 ms, submit+present 7.6 ms, GPU 20.5 ms; new pipelines 0 + shader libraries 0, 0.0 ms in all; texture uploads 0 first + 0 re-uploads, 0.0 MB / 0.0 ms; draws with a hybrid pipeline 0
[dm] [nvn] on-screen up to frame 3000: 300 frames, dropped 0 (discarded 0), p99 16.68 ms; footprint 1873 MiB; brightness -1.00; thermal 0
[dm] [nvn-mtl] parallel precompile: 363 draws left in this submission, 2 pipelines handed to the background
'''


class ReadLog(unittest.TestCase):
    def setUp(self):
        self.run_log = backend.read_log(LOG)

    def test_memory(self):
        self.assertEqual(self.run_log['peak_mb'], 1873)
        self.assertEqual(self.run_log['gpu_mb'], 1483)
        self.assertEqual(self.run_log['min_headroom_mb'], 230)
        self.assertEqual(self.run_log['last_frame'], 3000)

    def test_only_real_hitches_are_counted(self):
        self.assertEqual(self.run_log['hitches'], 2)
        self.assertEqual(self.run_log['worst_ms'], 556.1)
        self.assertEqual(self.run_log['texture_ms'], 284.6)
        self.assertEqual(self.run_log['pipeline_ms'], 31.8)

    def test_gpu_overruns(self):
        self.assertEqual(self.run_log['gpu_slow_frames'], 2)

    def test_last_line(self):
        self.assertTrue(self.run_log['ended_on_precompile'])

    def test_empty_log(self):
        empty = backend.read_log('')
        self.assertEqual((empty['peak_mb'], empty['hitches'], empty['min_headroom_mb']), (0, 0, None))


class Verdicts(unittest.TestCase):
    def test_textures_are_named_as_the_cause(self):
        text = ' '.join(backend.verdicts(backend.read_log(LOG), []))
        self.assertIn('mostly texture loading', text)

    def test_memory_kill_comes_first(self):
        kills = [{'time': '2026-11-02 11:52:39.00 -0700', 'reason': 'per-process-limit', 'mb': 2100}]
        out = backend.verdicts(backend.read_log(LOG), kills)
        self.assertIn('2,100 MB', out[0])

    def test_clean_run(self):
        out = backend.verdicts(backend.read_log('[dm] [nvn] on-screen up to frame 600: 300 frames, dropped 0 (discarded 0), p99 16.68 ms; footprint 900 MiB; thermal 0'), [])
        self.assertEqual(len(out), 1)
        self.assertIn('Nothing unusual', out[0])
        self.assertIn('closed by itself', out[0])


class Jetsam(unittest.TestCase):
    def test_reports_only_this_apps_kills(self):
        report = {'date': '2026-11-02 11:52:39', 'memoryStatus': {'pageSize': 16384}, 'processes': [
            {'name': 'DmIOS', 'rpages': 134400, 'reason': 'per-process-limit'}, {'name': 'cloudd', 'rpages': 100, 'reason': 'per-process-limit'}]}
        alive = {'date': '2026-11-02 11:53:13', 'memoryStatus': {'pageSize': 16384}, 'processes': [{'name': 'DmIOS', 'rpages': 110000}]}
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'JetsamEvent-1.ips').write_text('{"bug_type":"298"}\n' + json.dumps(report))
            (Path(folder) / 'JetsamEvent-2.ips').write_text('{"bug_type":"298"}\n' + json.dumps(alive))
            (Path(folder) / 'JetsamEvent-3.ips').write_text('not json')
            self.assertEqual(backend.read_jetsam(Path(folder)), [{'time': '2026-11-02 11:52:39', 'reason': 'per-process-limit', 'mb': 2100}])

    def test_newest_kill_last(self):
        def report(date): return '{"bug_type":"298"}\n' + json.dumps({'date': date, 'processes': [{'name': 'DmIOS', 'rpages': 134000, 'reason': 'per-process-limit'}]})
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / 'Retired').mkdir()
            (Path(folder) / 'JetsamEvent-2026-11-03-090346.ips').write_text(report('2026-11-03 09:03:46.65 -0700'))
            (Path(folder) / 'Retired/JetsamEvent-2026-11-02-135952.ips').write_text(report('2026-11-02 13:59:52.64 -0700'))
            self.assertEqual([kill['time'] for kill in backend.read_jetsam(Path(folder))], ['2026-11-02 13:59:52.64 -0700', '2026-11-03 09:03:46.65 -0700'])


class Failures(unittest.TestCase):
    def test_the_reason_survives_a_quiet_ending(self):
        script = "echo '[RED] This game needs a device'; echo noise; echo '** BUILD FAILED **'; exit 65"
        with self.assertRaises(backend.Failure) as caught:
            backend.stream(['/bin/sh', '-c', script], log_filter=lambda line: False)
        self.assertIn('needs a device', str(caught.exception))
        self.assertNotIn('noise', str(caught.exception))

    def test_a_failure_without_a_reason_shows_the_last_lines(self):
        with self.assertRaises(backend.Failure) as caught:
            backend.stream(['/bin/sh', '-c', 'echo last words; exit 1'], log_filter=lambda line: False)
        self.assertIn('last words', str(caught.exception))


class OneBuildAtATime(unittest.TestCase):
    def test_a_second_build_is_refused_and_the_lock_comes_back(self):
        with tempfile.TemporaryDirectory() as folder:
            original, backend.DATA = backend.DATA, Path(folder)
            try:
                first = backend.one_build_at_a_time()
                with self.assertRaises(backend.Failure):
                    backend.one_build_at_a_time()
                first.close()
                backend.one_build_at_a_time().close()
            finally:
                backend.DATA = original


class Standalone(unittest.TestCase):
    def make_bundle(self, folder, archive=None):
        bundle, source = Path(folder) / 'Resources', Path(folder) / 'source'
        (source / 'dm').mkdir(parents=True)
        bundle.mkdir()
        (source / 'dm/tool.py').write_text('print(1)\n')
        (bundle / 'engine.json').write_text(json.dumps({'id': 'abc123'}))
        if archive is None:
            subprocess.run(['/usr/bin/tar', '-cJf', bundle / 'engine.tar.xz', '-C', source, 'dm'], check=True)
        else:
            (bundle / 'engine.tar.xz').write_bytes(archive)
        return bundle

    def test_the_engine_is_unpacked_once_and_earlier_ones_go(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle, data = self.make_bundle(folder), Path(folder) / 'data'
            engine = backend.unpack_engine(bundle, data)
            self.assertEqual((engine / 'dm/tool.py').read_text(), 'print(1)\n')
            self.assertEqual(json.loads((engine / 'engine.json').read_text())['id'], 'abc123')
            (engine / 'dm/tool.py').write_text('changed by a build')
            self.assertEqual(backend.unpack_engine(bundle, data), engine)
            self.assertEqual((engine / 'dm/tool.py').read_text(), 'changed by a build')
            (data / 'engine/older').mkdir()
            backend.prune_engines(engine)
            self.assertEqual(sorted(p.name for p in (data / 'engine').iterdir() if p.is_dir()), ['abc123'])

    def test_a_broken_archive_leaves_no_engine(self):
        with tempfile.TemporaryDirectory() as folder:
            bundle, data = self.make_bundle(folder, archive=b'not an archive'), Path(folder) / 'data'
            with self.assertRaises(backend.Failure):
                backend.unpack_engine(bundle, data)
            self.assertEqual([p.name for p in (data / 'engine').iterdir() if p.is_dir()], [])

    def test_python_is_made_again_when_its_requirements_change(self):
        with tempfile.TemporaryDirectory() as folder:
            originals = backend.DATA, backend.REQUIREMENTS
            backend.DATA, backend.REQUIREMENTS = Path(folder), Path(folder) / 'requirements.txt'
            try:
                backend.REQUIREMENTS.write_text('lz4==1\n')
                self.assertFalse(backend.venv_ready())
                (backend.DATA / 'venv/bin').mkdir(parents=True)
                (backend.DATA / 'venv/bin/python3').write_text('')
                (backend.DATA / 'venv/tvport-requirements.txt').write_text(backend.python_stamp())
                self.assertTrue(backend.venv_ready())
                backend.REQUIREMENTS.write_text('lz4==2\n')
                self.assertFalse(backend.venv_ready())
            finally:
                backend.DATA, backend.REQUIREMENTS = originals

    def test_the_personal_team_of_a_signed_in_apple_id_is_found(self):
        prefs = {'IDEProvisioningTeamByIdentifier': {'account': [{'teamID': 'PAID123456', 'isFreeProvisioningTeam': False}, {'teamID': 'FREE123456', 'isFreeProvisioningTeam': True}]}}
        self.assertEqual(backend.xcode_teams(prefs), ['FREE123456', 'PAID123456'])
        self.assertEqual(backend.xcode_teams({}), [])

    def test_the_app_never_carries_game_files(self):
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
        import package_tvport
        bad = ['G/0100000000010000_build/extract/x.tsv', 'G/prod.keys', 'G/Game.nsp', 'dm/games/0100000000010000/c1_full.tsv', 'x/DmIOS.app/Info.plist', 'G/RomFS.img']
        good = ['dm/games/0100000000010000/game.tsv', 'dm/tools/nacp.py', 'scripts/build_ipa_from_nsp.py', 'LICENSE']
        self.assertEqual(package_tvport.unsafe(bad + good), bad)


class ShaderList(unittest.TestCase):
    def test_rows_merge_once_in_first_seen_order(self):
        self.assertEqual(backend.merged_rows('# header\nS\ta\t0\nR\tb\n', 'R\tb\nC\tc\n\n'), ['S\ta\t0', 'R\tb', 'C\tc'])

    def test_saved_rows_join_the_manifest_after_its_header(self):
        with tempfile.TemporaryDirectory() as folder:
            build, project = Path(folder), Path(folder) / 'project'
            (project / 'Gen/dm').mkdir(parents=True)
            (project / 'Gen/dm/pso_manifest.tsv').write_text('# header\nS\ta\t0\n')
            (build / backend.SHADER_LIST).write_text('S\ta\t0\nR\tb\n')
            game = type('G', (), {'build': build})()
            backend.bundle_shaders(game, project)
            self.assertEqual((project / 'Gen/dm/pso_manifest.tsv').read_text(), '# header\nS\ta\t0\nR\tb\n')

    def test_no_saved_list_leaves_the_manifest_alone(self):
        with tempfile.TemporaryDirectory() as folder:
            build, project = Path(folder), Path(folder) / 'project'
            (project / 'Gen/dm').mkdir(parents=True)
            (project / 'Gen/dm/pso_manifest.tsv').write_text('# header\n')
            backend.bundle_shaders(type('G', (), {'build': build})(), project)
            self.assertEqual((project / 'Gen/dm/pso_manifest.tsv').read_text(), '# header\n')


def listing(files):
    """What the TV lists for these saves: a change of content changes the size here."""
    return {name: [len(data), '2026-11-02T12:00:00.000Z'] for name, data in files.items()}


class Saves(unittest.TestCase):
    def setUp(self):
        self.real_icloud = backend.ICLOUD
        backend.ICLOUD = Path(tempfile.gettempdir()) / 'tvport-tests-no-icloud'   # iCloud Drive off, whatever this Mac has

    def tearDown(self):
        backend.ICLOUD = self.real_icloud

    def test_only_committed_saves_count(self):
        keep = ['save/File1.bin', 'save/sub/a.bin', 'save_device/x.bin', 'save.p-sam/File1.bin', 'save.p-sam-2_device/x.bin']
        drop = ['save.work/File1.bin', 'save.new/File1.bin', 'save.sync', 'save.sync-tmp/out/x', 'save-backups/2026/save/File1.bin', 'save.p-sam.work/File1.bin', 'romfs.img', 'save/']
        self.assertEqual([n for n in keep + drop if backend.SAVE_FILE.match(n)], keep)

    def make_game(self, folder):
        build = Path(folder) / '0100000000010000_build'
        build.mkdir()
        return backend.Game(Path(folder))

    def fake_device(self, files, copied=None):
        names, copied = list(files), copied if copied is not None else []

        def fake_run(argv, **kwargs):
            argv = [str(a) for a in argv]
            if 'from' in argv:
                source = argv[argv.index('--source') + 1].replace('Library/Caches/', '')
                target = Path(argv[argv.index('--destination') + 1])
                target.write_bytes(files[source])
            else:
                copied.append(argv[argv.index('--destination') + 1])
            return type('R', (), {'returncode': 0, 'stderr': '', 'stdout': ''})()
        return names, fake_run, copied

    def test_backup_then_identical_backup_then_change(self):
        files = {'save/File1.bin': b'one', 'save.p-sam/File1.bin': b'two'}
        copies = []
        _, fake_run, _ = self.fake_device(files)
        real_run, real_list = backend.run, backend.save_listing
        backend.run, backend.save_listing = lambda argv, **kw: copies.append(argv) or fake_run(argv, **kw), lambda d, b: listing(files)
        try:
            with tempfile.TemporaryDirectory() as folder:
                game = self.make_game(folder)
                first = backend.backup_saves(game, 'd', 'b')
                self.assertTrue(first['changed'])
                self.assertEqual((Path(first['folder']) / 'save.p-sam/File1.bin').read_bytes(), b'two')
                second = backend.backup_saves(game, 'd', 'b')
                self.assertEqual((second['changed'], second['state'], len(copies)), (False, 'unchanged', 2))   # the same listing: nothing copied
                self.assertEqual(len(list((game.build / 'save-backups').iterdir())), 1)
                files['save/File1.bin'] = b'three'
                self.assertTrue(backend.backup_saves(game, 'd', 'b')['changed'])   # in the same second as the first: it must not land on top of it
                self.assertEqual(len(list((game.build / 'save-backups').iterdir())), 2)
        finally:
            backend.run, backend.save_listing = real_run, real_list

    def test_a_failed_copy_leaves_no_backup(self):
        real_run, real_list = backend.run, backend.save_listing
        backend.run = lambda argv, **kwargs: type('R', (), {'returncode': 1, 'stderr': '', 'stdout': ''})()
        backend.save_listing = lambda d, b: listing({'save/File1.bin': b'x'})
        try:
            with tempfile.TemporaryDirectory() as folder:
                game = self.make_game(folder)
                with self.assertRaises(backend.Failure):
                    backend.backup_saves(game, 'd', 'b')
                self.assertIsNone(backend.latest_backup(game))
        finally:
            backend.run, backend.save_listing = real_run, real_list

    def test_old_backups_are_trimmed(self):
        with tempfile.TemporaryDirectory() as folder:
            game = self.make_game(folder)
            for i in range(backend.KEEP_BACKUPS):
                old = game.build / f'save-backups/2020-01-{i:02d}/save'
                old.mkdir(parents=True)
                (old / 'File1.bin').write_bytes(b'old')
            _, fake_run, _ = self.fake_device({'save/File1.bin': b'new'})
            real_run, real_list = backend.run, backend.save_listing
            backend.run, backend.save_listing = fake_run, lambda d, b: listing({'save/File1.bin': b'new'})
            try:
                backend.backup_saves(game, 'd', 'b')
            finally:
                backend.run, backend.save_listing = real_run, real_list
            kept = sorted(p.name for p in (game.build / 'save-backups').iterdir())
            self.assertEqual(len(kept), backend.KEEP_BACKUPS)
            self.assertNotIn('2020-01-00', kept)

    def test_no_saves_on_the_tv_means_no_backup(self):
        real = backend.save_listing
        try:
            with tempfile.TemporaryDirectory() as folder:
                game = self.make_game(folder)
                backend.save_listing = lambda d, b: {}
                self.assertEqual(backend.backup_saves(game, 'd', 'b')['state'], 'none')
                backend.save_listing = lambda d, b: None
                self.assertEqual(backend.backup_saves(game, 'd', 'b')['state'], 'unavailable')   # a TV that did not answer
        finally:
            backend.save_listing = real

    def test_a_save_during_the_copy_is_thrown_away(self):
        files = {'save/File1.bin': b'one'}
        answers = [listing(files), listing({'save/File1.bin': b'newer'})]   # the game committed between the two listings
        _, fake_run, _ = self.fake_device(files)
        real_run, real_list = backend.run, backend.save_listing
        backend.run, backend.save_listing = fake_run, lambda d, b: answers.pop(0) if answers else listing(files)
        try:
            with tempfile.TemporaryDirectory() as folder:
                game = self.make_game(folder)
                result = backend.backup_saves(game, 'd', 'b', attempts=1)
                self.assertEqual(result['state'], 'busy')
                self.assertIsNone(backend.latest_backup(game))
                self.assertEqual([p.name for p in game.build.iterdir() if p.name.startswith('.save-backup-incoming')], [])
        finally:
            backend.run, backend.save_listing = real_run, real_list

    def test_manifests_compare_size_and_time(self):
        files = {'save/File1.bin': b'one'}
        _, fake_run, _ = self.fake_device(files)
        stamp = ['2026-11-02T12:00:00.000Z']
        real_run, real_list = backend.run, backend.save_listing
        backend.run, backend.save_listing = fake_run, lambda d, b: {'save/File1.bin': [len(files['save/File1.bin']), stamp[0]]}
        try:
            with tempfile.TemporaryDirectory() as folder:
                game = self.make_game(folder)
                first = backend.backup_saves(game, 'd', 'b')
                self.assertEqual(backend.read_manifest(Path(first['folder'])), {'save/File1.bin': [3, stamp[0]]})
                stamp[0] = '2026-11-02T12:05:00.000Z'   # written again with the same bytes: copied, compared, and no new backup
                self.assertEqual(backend.backup_saves(game, 'd', 'b')['state'], 'unchanged')
                self.assertEqual(backend.read_manifest(Path(first['folder'])), {'save/File1.bin': [3, stamp[0]]})
                files['save/File1.bin'] = b'two'   # same size, new time, new bytes
                stamp[0] = '2026-11-02T12:10:00.000Z'
                self.assertEqual(backend.backup_saves(game, 'd', 'b')['state'], 'backed-up')
        finally:
            backend.run, backend.save_listing = real_run, real_list

    def test_retention_keeps_twenty_and_one_a_day(self):
        today = backend.datetime(2026, 11, 2).date()
        recent = [f'2026-11-02-{h:02d}{m:02d}00' for h in range(10, 12) for m in range(0, 60, 5)]   # 24 rounds this morning
        daily = [f'2026-10-{d:02d}-{t}' for d in range(5, 32) for t in ('080000', '200000')]
        old = ['2026-10-03-200000', '2025-01-01-120000']
        kept = backend.kept_backups(recent + daily + old + ['notes'], today)
        self.assertEqual(sorted(kept)[-20:], recent[-20:])
        self.assertEqual({n for n in kept if not n.startswith('2026-11-02')}, {f'2026-10-{d:02d}-200000' for d in range(5, 32)})   # 30 days back: the newest of each day
        self.assertEqual(len(kept), 20 + 27)

    def test_the_newest_backup_of_both_places_wins(self):
        with tempfile.TemporaryDirectory() as folder:
            game = self.make_game(folder)
            mac, cloud = game.build / 'save-backups', Path(folder) / 'iCloud/TVPort Saves' / game.title_id
            for place, name in ((mac, '2026-11-02-100000'), (mac, '2026-11-02-120000'), (cloud, '2026-11-02-110000'), (cloud, '2026-11-02-120000-2'), (cloud, '.2026-11-02-130000.partial')):
                (place / name / 'save').mkdir(parents=True)
            self.assertEqual(backend.latest_backup(game), mac / '2026-11-02-120000')   # iCloud Drive off: only this Mac's
            backend.ICLOUD = Path(folder) / 'iCloud'
            self.assertEqual(backend.latest_backup(game), cloud / '2026-11-02-120000-2')
            self.assertEqual(backend.backup_time(backend.latest_backup(game)), backend.datetime(2026, 11, 2, 12).timestamp())

    def test_icloud_backups_go_to_icloud_drive(self):
        files = {'save/File1.bin': b'one'}
        _, fake_run, _ = self.fake_device(files)
        real_run, real_list = backend.run, backend.save_listing
        backend.run, backend.save_listing = fake_run, lambda d, b: listing(files)
        try:
            with tempfile.TemporaryDirectory() as folder:
                game = self.make_game(folder)
                backend.ICLOUD = Path(folder) / 'iCloud'
                backend.ICLOUD.mkdir()
                result = backend.backup_saves(game, 'd', 'b', icloud=True)
                self.assertEqual(Path(result['folder']).parent, backend.ICLOUD / 'TVPort Saves/0100000000010000')
                self.assertFalse((game.build / 'save-backups').exists())
        finally:
            backend.run, backend.save_listing = real_run, real_list

    def test_restore_only_when_the_tv_has_lost_its_saves(self):
        copied = []
        _, fake_run, _ = self.fake_device({}, copied)
        real_run, real_list = backend.run, backend.save_files
        backend.run = fake_run
        try:
            with tempfile.TemporaryDirectory() as folder:
                game = self.make_game(folder)
                saved = game.build / 'save-backups/2026-11-02-150000/save'
                saved.mkdir(parents=True)
                (saved / 'File1.bin').write_bytes(b'x')
                backend.save_files = lambda d, b: ['save/File1.bin']
                self.assertEqual(backend.restore_saves(game, 'd', 'b'), 0)
                self.assertEqual(copied, [])
                backend.save_files = lambda d, b: []
                self.assertEqual(backend.restore_saves(game, 'd', 'b'), 1)
                self.assertEqual(copied, ['Library/Caches/save/File1.bin'])
        finally:
            backend.run, backend.save_files = real_run, real_list

    def test_players_reach_the_app(self):
        with tempfile.TemporaryDirectory() as folder:
            game = self.make_game(folder)
            self.assertIn('DM_TV_PLAYERS=Taylor, Sam\n', backend.write_env(game, 'handheld', 512, ' Taylor,  Sam ').read_text())
            self.assertNotIn('DM_TV_PLAYERS', backend.write_env(game, 'handheld', 512, '').read_text())

    def test_render_scale_reaches_the_app(self):
        with tempfile.TemporaryDirectory() as folder:
            game = self.make_game(folder)
            self.assertIn('DM_RENDER_SCALE=0.75\n', backend.write_env(game, 'handheld', 512, '', 0.75).read_text())
            self.assertNotIn('DM_RENDER_SCALE', backend.write_env(game, 'handheld', 512, '').read_text())   # full size: the game's own, dynamic resolution included
            self.assertEqual(backend.render_scale_arg('0.6'), 0.6)
            with self.assertRaises(Exception): backend.render_scale_arg('0.4')

    def test_render_scale_offered_only_with_the_stub(self):
        with tempfile.TemporaryDirectory() as folder:
            game = self.make_game(folder)
            self.assertFalse(game.render_scale_supported())   # not rewritten yet
            patched = game.build / 'patched_v720896'; patched.mkdir()
            (patched / 'main.fixups.tsv').write_text('# FPS_STATE_OFF\t0x3ad4000\n')
            self.assertFalse(backend.Game(Path(folder)).render_scale_supported())
            (patched / 'main.fixups.tsv').write_text('# FPS_STATE_OFF\t0x3ad4000\n# RENDER_SCALE\t1\n')
            self.assertTrue(backend.Game(Path(folder)).render_scale_supported())
            self.assertTrue(backend.Game(Path(folder)).frame_step_supported())

    def test_frame_rate_target_reaches_the_app(self):
        with tempfile.TemporaryDirectory() as folder:
            game = self.make_game(folder)
            self.assertIn('DM_FPS_TARGET=30\n', backend.write_env(game, 'handheld', 512, '', 1.0, True, 30).read_text())
            self.assertNotIn('DM_FPS_TARGET', backend.write_env(game, 'handheld', 512, '').read_text())   # the profile's own
            self.assertEqual(game.profile_value('fps_target'), '')   # Odyssey's profile has no frame-rate target
            (game.build / 'profile/0100000000010000').mkdir(parents=True)
            (game.build / 'profile/0100000000010000/game.tsv').write_text('title_id\t0100000000010000\nfps_target\t60\n')
            self.assertEqual(backend.Game(Path(folder)).profile_value('fps_target'), '60')   # a derived profile wins

    def test_thermal_cap_can_be_left_off(self):
        with tempfile.TemporaryDirectory() as folder:
            game = self.make_game(folder)
            self.assertIn('DM_THERMAL_CAP=0\n', backend.write_env(game, 'handheld', 512, '', 1.0, False).read_text())
            self.assertNotIn('DM_THERMAL_CAP', backend.write_env(game, 'handheld', 512, '').read_text())


class FindKeys(unittest.TestCase):
    def test_keys_beside_the_game_win(self):
        with tempfile.TemporaryDirectory() as folder:
            game = Path(folder) / 'Game'
            game.mkdir()
            (Path(folder) / 'prod.keys').write_text('x')
            self.assertEqual(backend.find_keys(backend.Game(game)).name, 'prod.keys')


class Launching(unittest.TestCase):
    def test_finds_only_this_game(self):
        listing = {'result': {'runningProcesses': [
            {'processIdentifier': 7, 'executable': 'file:///private/var/containers/Bundle/Application/X/DmIOS.app/DmIOS'},
            {'processIdentifier': 8, 'executable': 'file:///Applications/HeadBoard.app/HeadBoard'},
            {'processIdentifier': 9}]}}
        self.assertEqual(backend.running_pids(listing), [7])


class Devices(unittest.TestCase):
    def test_keeps_real_apple_tvs_only(self):
        listing = {'result': {'devices': [
            {'hardwareProperties': {'deviceType': 'appleTV', 'reality': 'simulated', 'udid': 'SIM'}},
            {'hardwareProperties': {'deviceType': 'iPhone', 'reality': 'physical', 'udid': 'PHONE'}},
            {'hardwareProperties': {'deviceType': 'appleTV', 'reality': 'physical', 'udid': 'TV', 'marketingName': 'Apple TV 4K (3rd generation)'},
             'deviceProperties': {'name': 'Basement', 'osVersionNumber': '27.2', 'developerModeStatus': 'enabled'}, 'connectionProperties': {'tunnelState': 'connected'}}]}}
        tvs = backend.apple_tvs(listing)
        self.assertEqual([t['id'] for t in tvs], ['TV'])
        self.assertTrue(tvs[0]['connected'] and tvs[0]['developer_mode'])


class GameFolder(unittest.TestCase):
    def make(self, folder, *paths):
        for path in paths:
            target = Path(folder) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text('')

    def test_new_game_is_not_prepared(self):
        with tempfile.TemporaryDirectory() as folder:
            self.make(folder, 'Game.nsp', 'cover.png')
            game = backend.Game(Path(folder))
            self.assertEqual(game.sources, ['Game.nsp'])
            self.assertFalse(game.prepared)

    def test_built_game_is_found(self):
        chain = backend.chain()
        with tempfile.TemporaryDirectory() as folder:
            self.make(folder, '0100000000010000_build/patched_v1/main.patched.bin', '0100000000010000_build/extract/v1/RomFS.img', f'0100000000010000_build/shader/ios/b-{chain}/inputs.tsv')
            game = backend.Game(Path(folder))
            self.assertEqual((game.title_id, game.package), ('0100000000010000', 'v1'))
            self.assertTrue(game.prepared)

    def test_shaders_of_another_chain_do_not_count(self):
        with tempfile.TemporaryDirectory() as folder:
            self.make(folder, '0100000000010000_build/patched_v1/main.patched.bin', '0100000000010000_build/extract/v1/RomFS.img', '0100000000010000_build/shader/ios/b-000000000000/inputs.tsv')
            self.assertFalse(backend.Game(Path(folder)).prepared)

    def built(self, folder):
        self.make(folder, 'Game.nsp', f'0100000000010000_build/patched_v1/main.patched.bin', '0100000000010000_build/extract/v1/RomFS.img', f'0100000000010000_build/shader/ios/b-{backend.chain()}/inputs.tsv')
        return backend.Game(Path(folder))

    def test_a_dlc_added_after_the_build_asks_the_engine_to_prepare_again(self):
        with tempfile.TemporaryDirectory() as folder:
            game = self.built(folder)
            self.assertTrue(game.prepared)
            game.save_state(sources=game.fingerprint)
            self.assertTrue(backend.Game(Path(folder)).prepared)
            (Path(folder) / 'DLC.nsp').write_text('dlc')
            self.assertFalse(backend.Game(Path(folder)).prepared)
            game = backend.Game(Path(folder))
            game.save_state(sources=game.fingerprint)
            self.assertTrue(backend.Game(Path(folder)).prepared)

    def test_a_build_without_a_noted_file_list_counts_as_current(self):
        with tempfile.TemporaryDirectory() as folder:
            self.built(folder)
            (Path(folder) / 'DLC.nsp').write_text('dlc')
            self.assertTrue(backend.Game(Path(folder)).prepared)

    def test_new_bundle_id_comes_from_team_and_title(self):
        with tempfile.TemporaryDirectory() as folder:
            self.make(folder, '0100ABCD00000000_build/x')
            self.assertEqual(backend.Game(Path(folder)).bundle_id('TEAM123'), 'dev.tvport.team123.g0100abcd00000000')


if __name__ == '__main__':
    unittest.main()
