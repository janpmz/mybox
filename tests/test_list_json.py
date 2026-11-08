"""Exercise JSON output through the CLI with disposable backend and disk data."""
import json
import os
import uuid
import unittest

import test_cli


class ListJSON(unittest.TestCase):
    setUp = test_cli.CLI.setUp
    run_box = test_cli.CLI.run_box
    calls = test_cli.CLI.calls

    def test_numeric_resources_and_both_port_sources(self):
        disk = self.container_root / 'mybox-demo' / 'rootfs.ext4'
        disk.parent.mkdir(parents=True)
        with disk.open('wb') as stream:
            stream.write(os.urandom(4096))
            stream.truncate(8 * 1024 ** 3)
        config = dict(labels={'io.github.janpmz.my-container-box.managed': 'true'},
                      creationDate='2026-11-04T10:00:00Z',
                      resources=dict(cpus=6, memoryInBytes=512 * 1024 ** 2),
                      publishedPorts=[dict(hostPort=8766, containerPort=8080)])
        self.log.with_suffix('.state').write_text(json.dumps({'mybox-demo': dict(
            id='mybox-demo', configuration=config, status=dict(state='stopped'))}))
        ports = self.directory / 'ports' / 'mybox-demo'
        ports.mkdir(parents=True)
        (ports / 'ports.json').write_text(json.dumps(dict(
            version=1, id='mybox-demo', created=config['creationDate'],
            mappings=[dict(inside=3000, outside=8767)], revision=str(uuid.uuid4()))))
        result = self.run_box('list', '--json')
        self.assertEqual(json.loads(result.stdout), dict(schemaVersion=1, boxes=[dict(
            name='demo', state='stopped', cpus=6, memoryBytes=512 * 1024 ** 2,
            diskUsedBytes=disk.stat().st_blocks * 512, diskCapacityBytes=8 * 1024 ** 3,
            ports=[dict(inside=8080, outside=8766), dict(inside=3000, outside=8767)])]))
        self.assertEqual(result.stderr, '')
        self.assertEqual(self.calls(), [['list', '--all', '--format', 'json']])

    def test_filters_sorts_and_preserves_unknown_values(self):
        records = {name: dict(id=name, configuration=dict(labels=labels), status=dict(state='running'))
                   for name, labels in [
                       ('mybox-zulu', {'io.github.janpmz.my-container-box.managed': 'true'}),
                       ('mybox-alpha', {'io.github.janpmz.my-container-box.managed': 'true'}),
                       ('mybox-foreign', {}),
                       ('mybox-helper-test', {'io.github.janpmz.my-container-box.helper': 'true'})]}
        self.log.with_suffix('.state').write_text(json.dumps(records))
        disk = self.container_root / 'mybox-alpha' / 'rootfs.ext4'
        disk.parent.mkdir(parents=True)
        disk.symlink_to(self.log.with_suffix('.state'))
        result = json.loads(self.run_box('list', '--json').stdout)
        self.assertEqual([box['name'] for box in result['boxes']], ['alpha', 'zulu'])
        for box in result['boxes']:
            for field in ['cpus', 'memoryBytes', 'diskUsedBytes', 'diskCapacityBytes']:
                self.assertIn(field, box)
                self.assertIsNone(box[field])
            self.assertEqual(box['ports'], [])
        self.assertEqual(self.calls(), [['list', '--all', '--format', 'json']])

    def test_empty_list_is_json_without_human_message(self):
        self.log.with_suffix('.state').write_text('{}')
        self.assertEqual(json.loads(self.run_box('list', '--json').stdout), dict(schemaVersion=1, boxes=[]))
        self.assertIn('No MyBox boxes yet.', self.run_box('list').stdout)

    def test_startup_diagnostic_stays_on_stderr(self):
        self.log.with_suffix('.service').write_text('unregistered')
        result = self.run_box('list', '--json')
        self.assertEqual(json.loads(result.stdout)['boxes'][0]['name'], 'demo')
        self.assertIn("Starting Apple's container service", result.stderr)
        self.assertEqual(self.calls(), [['list', '--all', '--format', 'json']])

    def test_invalid_arguments_do_not_reach_backend(self):
        for arguments in [('list', '--json', '--json'), ('list', '--json', 'demo'),
                          ('list', '--yaml'), ('list', '--all', '--json')]:
            with self.subTest(arguments=arguments):
                result = self.run_box(*arguments, success=False)
                self.assertEqual(result.stdout, '')
                self.assertIn('Usage: mybox list [--json]', result.stderr)
        self.assertEqual(self.calls(), [])

    def test_backend_failure_is_not_an_empty_success(self):
        self.environment['MYBOX_TEST_BACKEND_FAIL'] = '["list"]'
        result = self.run_box('list', '--json', success=False)
        self.assertEqual(result.stdout, '')


if __name__ == '__main__':
    unittest.main()
